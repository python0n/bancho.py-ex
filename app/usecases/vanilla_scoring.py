"""Transactional score selection and stat bookkeeping for vanilla osu! modes.

Does not calculate individual pp. RX/AP must continue using the existing paths.

The integration in osu.py and replay_approval.py intentionally remains a
separate review step; the pure selection and weighting functions below can be
regression-tested without importing the running server.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import contextlib
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class ScoredPlay:
    id: int
    pp: Decimal
    score: int


def select_vanilla_bests(plays: Iterable[ScoredPlay]) -> tuple[int | None, int | None]:
    """Return (pp_best_id, score_best_id), stable ties go to the older id.

    Exactly one pp-best; status 3 only for a *different* score-best play.
    Status 0 / failed records must be filtered out by the database query.
    """
    accepted = list(plays)
    if not accepted:
        return None, None
    best_pp = max(accepted, key=lambda p: (p.pp, -p.id))
    best_score = max(accepted, key=lambda p: (p.score, -p.id))
    return best_pp.id, best_score.id if best_score.id != best_pp.id else None


def weighted_legacy_pp(plays_pp: Iterable[Decimal]) -> int:
    """Keep TSG's legacy 0.95 weighting and 416.6667 bonus, unchanged."""
    pp = sorted((float(x) for x in plays_pp), reverse=True)
    return round(sum(value * 0.95 ** i for i, value in enumerate(pp)) +
                 416.6667 * (1 - 0.9994 ** len(pp)))


def weighted_legacy_acc(plays_acc: Iterable[float]) -> float:
    """Mirror the old acc calculation; explicitly handle an empty set."""
    acc = list(plays_acc)
    if not acc:
        return 0.0
    weighted_acc = sum(float(value) * 0.95 ** i for i, value in enumerate(acc))
    bonus_acc = 100.0 / (20 * (1 - 0.95 ** len(acc)))
    return (weighted_acc * bonus_acc) / 100


def vanilla_ranked_score(best_scores_on_ranked_approved_loved: Iterable[int]) -> int:
    """Sum per-map highest raw score. Caller chooses map status (2,3,5)."""
    return sum(best_scores_on_ranked_approved_loved)


class DuplicateVanillaScore(Exception):
    """The checksum was already committed (including a concurrent request)."""


# Held through the SQL commit AND the in-memory/Redis refresh. SQL row locks
# handle other processes; this extra lock prevents stale cache overwrites in
# the current one-process deployment. Entries are ref-counted and cleaned up.
_player_locks: dict[tuple[int, int], list] = {}


@contextlib.asynccontextmanager
async def _player_mode_lock(user_id: int, mode: int) -> AsyncIterator[None]:
    key = (user_id, mode)
    entry = _player_locks.get(key)
    if entry is None:
        entry = _player_locks[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _player_locks.pop(key, None)


async def _announce_global_rank(db, player, transition):
    """Best effort only: never retry a committed score for a chat failure."""
    if transition is None:
        return
    import app.state
    from app.logging import Ansi, log
    try:
        before, after, displaced_id = transition
        if player.restricted or not (0 < after <= 50 and (before == 0 or after < before)):
            return
        channel = app.state.sessions.channels.get_by_name("#announce")
        if channel is None:
            return
        message = f"\x01ACTION achieved global rank #{after}"
        if displaced_id is not None and displaced_id != player.id:
            try:
                displaced = app.state.sessions.players.get(id=displaced_id)
                row = None if displaced else await db.fetch_one(
                    "SELECT name FROM users WHERE id = :id", {"id": displaced_id},
                )
                name = displaced.name if displaced else (row["name"] if row else None)
                if name:
                    message += f" (Previous: {name})"
            except Exception:
                pass  # Optional name; do not lose the base message.
        channel.send(message, sender=player, to_self=True)
    except Exception as exc:
        log(f"[vanilla-scoring] global rank announce failed: {type(exc).__name__}", Ansi.LRED)


async def commit_vanilla_score(score, *, pending_id: int | None = None, replay_intent=None, announce_global_rank=False):
    from app.constants.gamemodes import GameMode
    assert score.player is not None
    if score.mode >= GameMode.RELAX_OSU:
        raise ValueError('RX/AP must remain on the legacy pipeline')
    # A reviewed pending vanilla replay may carry a durable staged copy.
    # Its link is committed atomically with the score and pending approval.
    if replay_intent is not None and (
        not score.passed or replay_intent.online_checksum != str(score.client_checksum).lower()
    ):
        raise ValueError("replay intent checksum/status mismatch")
    async with _player_mode_lock(int(score.player.id), int(score.mode)):
        return await _commit_vanilla_score_inner(
            score, pending_id=pending_id, replay_intent=replay_intent,
            announce_global_rank=announce_global_rank and pending_id is None,
        )


async def _commit_vanilla_score_inner(score, *, pending_id: int | None = None, replay_intent=None, announce_global_rank=False):
    """Store a vanilla score and its SQL stats under one per-user row lock.

    This function must only be called for modes 0..3, from under the existing
    in-process checksum lock. All MySQL writes are in one transaction; Redis
    and .osr writes intentionally stay outside SQL and must be retried/reconciled
    by the caller if needed.

    Returns a COPY of the pre-submit in-memory stats, for submission charts.
    """
    import copy

    import app.state
    from app.constants.gamemodes import GameMode
    from app.objects.score import Grade, Score, SubmissionStatus
    from app.api.domains.osu import pp_to_db, pp_to_float

    assert score.player is not None and score.bmap is not None
    if score.mode >= GameMode.RELAX_OSU:
        raise ValueError('vanilla scorer must not be used for RX/AP')

    db = app.state.services.database
    player = score.player
    bmap = score.bmap
    mode = int(score.mode)
    keys = {'user_id': player.id, 'mode': mode, 'map_md5': bmap.md5}
    player_mode_keys = {'user_id': player.id, 'mode': mode}
    previous_memory_stats = copy.copy(player.stats[score.mode])
    old_pp_best_id = None
    pp_best_changed = False

    params = dict(
        map_md5=bmap.md5, score=score.score, pp=pp_to_db(pp_to_float(score.pp)),
        acc=score.acc, max_combo=score.max_combo, mods=int(score.mods),
        n300=score.n300, n100=score.n100, n50=score.n50,
        nmiss=score.nmiss, ngeki=score.ngeki, nkatu=score.nkatu,
        grade=score.grade.name,
        status=int(SubmissionStatus.SUBMITTED if score.passed else SubmissionStatus.FAILED),
        mode=mode, play_time=score.server_time, time_elapsed=score.time_elapsed,
        client_flags=int(score.client_flags), user_id=player.id,
        perfect=int(score.perfect), checksum=score.client_checksum,
    )
    insert_sql = (
        'INSERT INTO scores VALUES (NULL, '
        ':map_md5, :score, :pp, :acc, :max_combo, :mods, '
        ':n300, :n100, :n50, :nmiss, :ngeki, :nkatu, '
        ':grade, :status, :mode, :play_time, :time_elapsed, '
        ':client_flags, :user_id, :perfect, :checksum, 0, 0)'
    )
    final_stats = None

    async with db.transaction():
        # Lock the user for the ENTIRE selection, insert, score status update,
        # and stats update; also serializes different-checksum submissions.
        locked_user = await db.fetch_one(
            'SELECT id FROM users WHERE id = :user_id FOR UPDATE',
            {'user_id': player.id},
        )
        if locked_user is None:
            raise RuntimeError('player no longer exists')

        # Repeat the checksum check UNDER the cross-worker SQL row lock.
        if await db.fetch_one(
            'SELECT id FROM scores WHERE online_checksum = :checksum LIMIT 1',
            {'checksum': score.client_checksum},
        ):
            raise DuplicateVanillaScore(score.client_checksum)

        if pending_id is not None:
            pending = await db.fetch_one(
                'SELECT id FROM pending_replays WHERE id = :id AND status = 0 FOR UPDATE',
                {'id': pending_id},
            )
            if pending is None:
                raise DuplicateVanillaScore(f'pending replay {pending_id} already processed')

        old_pp = await db.fetch_one(
            'SELECT id FROM scores WHERE userid=:user_id AND map_md5=:map_md5 '
            'AND mode=:mode AND status=2 ORDER BY pp DESC, id ASC LIMIT 1',
            keys,
        )
        old_pp_best_id = int(old_pp['id']) if old_pp else None
        prev_raw = await db.fetch_one(
            'SELECT MAX(score) AS highest FROM scores '
            'WHERE userid=:user_id AND map_md5=:map_md5 AND mode=:mode '
            'AND status IN (1,2,3)', keys,
        )
        old_max_score = prev_raw['highest'] if prev_raw else None

        score.id = await db.execute(insert_sql, params)
        if replay_intent is not None:
            # Same InnoDB transaction as scores, stats and rank-sync outbox.
            from app.usecases.vanilla_replay_links_preview import insert_replay_link
            await insert_replay_link(db, score.id, replay_intent)
        if score.passed:
            # Include historical SUBMITTED plays: otherwise an old broken
            # status can continue to skew BEST/SCORE_BEST indefinitely.
            rows = await db.fetch_all(
                'SELECT id, pp, score FROM scores '
                'WHERE userid=:user_id AND map_md5=:map_md5 AND mode=:mode '
                'AND status IN (1,2,3)', keys,
            )
            best_pp_id, score_best_id = select_vanilla_bests(
                ScoredPlay(int(row['id']), Decimal(str(row['pp'])), int(row['score']))
                for row in rows
            )
            await db.execute(
                'UPDATE scores SET status=1 WHERE userid=:user_id '
                'AND map_md5=:map_md5 AND mode=:mode AND status IN (2,3)', keys,
            )
            if best_pp_id is not None:
                await db.execute('UPDATE scores SET status=2 WHERE id=:id', {'id': best_pp_id})
            if score_best_id is not None:
                await db.execute('UPDATE scores SET status=3 WHERE id=:id', {'id': score_best_id})

            score.status = (
                SubmissionStatus.BEST if score.id == best_pp_id
                else SubmissionStatus.SCORE_BEST if score.id == score_best_id
                else SubmissionStatus.SUBMITTED
            )
            score.leaderboard_best = (
                old_max_score is None or score.score > int(old_max_score)
            )
            pp_best_changed = old_pp_best_id != best_pp_id
        else:
            score.status = SubmissionStatus.FAILED
            score.leaderboard_best = False

        if pending_id is not None:
            await db.execute(
                'UPDATE pending_replays SET status=1, reviewed_at=NOW(), '
                'score_id=:score_id WHERE id=:id AND status=0',
                {'score_id': score.id, 'id': pending_id},
            )

        stat = await db.fetch_one(
            'SELECT * FROM stats WHERE id=:user_id AND mode=:mode FOR UPDATE', player_mode_keys,
        )
        if stat is None:
            raise RuntimeError('missing stats row for player and mode')

        updates = dict(
            plays=int(stat['plays']) + 1,
            playtime=int(stat['playtime']) + score.time_elapsed // 1000,
            tscore=int(stat['tscore']) + score.score,
            total_hits=(int(stat['total_hits']) + score.n300 + score.n100 + score.n50
                        + (score.ngeki + score.nkatu if mode in (1,3) else 0)),
            max_combo=(max(int(stat['max_combo']), score.max_combo)
                       if score.passed and bmap.has_leaderboard else int(stat['max_combo'])),
            rscore=int(stat['rscore']), pp=int(stat['pp']), acc=float(stat['acc']),
        )
        for grade in ('xh', 'x', 'sh', 's', 'a'):
            updates[f'{grade}_count'] = int(stat[f'{grade}_count'])

        if score.passed and bmap.has_leaderboard:
            ranked = await db.fetch_one(
                'SELECT COALESCE(SUM(per_map.best_score), 0) AS total FROM ('
                '  SELECT MAX(s.score) AS best_score FROM scores s '
                '  INNER JOIN maps m ON m.md5=s.map_md5 '
                '  WHERE s.userid=:user_id AND s.mode=:mode '
                '  AND s.status IN (1,2,3) AND m.status IN (2,3,5) '
                '  GROUP BY s.map_md5'
                ') per_map', player_mode_keys,
            )
            updates['rscore'] = int(ranked['total'] or 0)

        if score.passed and bmap.awards_ranked_pp and pp_best_changed:
            plays = await db.fetch_all(
                'SELECT s.pp, s.acc FROM scores s '
                'JOIN maps m ON m.md5=s.map_md5 '
                'WHERE s.userid=:user_id AND s.mode=:mode '
                'AND s.status=2 AND m.status IN (2,3) '
                'ORDER BY s.pp DESC', player_mode_keys,
            )
            # EXACT same weighted formulas/bonus as the existing TSG code.
            values = [float(p['pp']) for p in plays]
            weighted = sum(v * 0.95**i for i, v in enumerate(values))
            bonus = 416.6667 * (1 - 0.9994**len(values))
            updates['pp'] = round(weighted + bonus)
            acc_values = [float(p['acc']) for p in plays]
            updates['acc'] = weighted_legacy_acc(acc_values)

            grades = await db.fetch_all(
                'SELECT s.grade, COUNT(*) AS c FROM scores s '
                'JOIN maps m ON m.md5=s.map_md5 '
                'WHERE s.userid=:user_id AND s.mode=:mode AND s.status=2 '
                'AND m.status IN (2,3) GROUP BY s.grade', player_mode_keys,
            )
            for key in ('xh', 'x', 'sh', 's', 'a'):
                updates[f'{key}_count'] = 0
            for row in grades:
                col = f"{str(row['grade']).lower()}_count"
                if col in updates:
                    updates[col] = int(row['c'])

        update_columns = sorted(updates)
        assignments = ', '.join(f'{name}=:{name}' for name in update_columns)
        await db.execute(
            f'UPDATE stats SET {assignments} WHERE id=:user_id AND mode=:mode',
            {**player_mode_keys, **updates},
        )

        if pp_best_changed and bmap.awards_ranked_pp:
            # Enqueue inside score+stats transaction. A crash after COMMIT
            # can no longer silently lose the Redis update.
            from app.usecases.vanilla_rank_sync import enqueue_vanilla_rank_sync
            await enqueue_vanilla_rank_sync(db, player.id, mode)

        # Map counters are also incremented atomically, not with stale values.
        if not player.restricted:
            await db.execute(
                'UPDATE maps SET plays=plays+1, passes=passes+:passed '
                'WHERE md5=:map_md5',
                {'map_md5': bmap.md5, 'passed': int(score.passed)},
            )
        final_stats = updates

    # Only update the cache AFTER the SQL transaction has committed.
    # Redis and notifications intentionally run after this point.
    assert final_stats is not None
    cache = player.stats[score.mode]
    for attr in ('plays', 'playtime', 'tscore', 'total_hits', 'max_combo', 'rscore', 'pp', 'acc'):
        setattr(cache, attr, final_stats[attr])
    for grade in (Grade.XH, Grade.X, Grade.SH, Grade.S, Grade.A):
        cache.grades[grade] = final_stats[f'{grade.name.lower()}_count']

    if not player.restricted:
        bmap.plays += 1
        if score.passed:
            bmap.passes += 1

    if old_pp_best_id is not None:
        # Charts only; errors here must never roll back a committed score.
        try:
            score.prev_best = await Score.from_sql(old_pp_best_id)
        except Exception:
            score.prev_best = None

    if score.passed and bmap.has_leaderboard:
        try:
            score.rank = await score.calculate_placement()
        except Exception:
            score.rank = None

    if pp_best_changed and bmap.awards_ranked_pp:
        # Fast path: try immediately, but the durable outbox will retry if
        # Redis is down, the process exits, or SQL acknowledgement fails.
        from app.usecases.vanilla_rank_sync import sync_vanilla_rank
        try:
            transition = None
            if announce_global_rank and not player.restricted:
                from app.usecases.vanilla_rank_sync import sync_vanilla_rank_with_transition
                rank, transition = await sync_vanilla_rank_with_transition(
                    db, app.state.services.redis, player.id, mode,
                )
            else:
                rank = await sync_vanilla_rank(db, app.state.services.redis, player.id, mode)
            if rank is not None:
                cache.rank = rank
            if transition is not None:
                await _announce_global_rank(db, player, transition)
        except Exception as exc:
            from app.logging import Ansi, log
            log(
                f'[vanilla-scoring] rank sync queued for retry ({type(exc).__name__})',
                Ansi.LRED,
            )

    return previous_memory_stats
