"""Saving a replay that a player uploaded on the website, after an admin
accepted it in Shiina's admin panel (/ap/replays).

The score goes through the same steps as a score sent from the game: mode with
Relax/Autopilot, accuracy, pp, best score status (pp best and score best),
the transactional insert that keeps one best score per map, the player's stats
(pp, accuracy, ranked score, grades, rank) and the in-game notifications.
"""

from __future__ import annotations

import lzma
import shutil
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

import app.packets
import app.settings
import app.state
from app._typing import UNSET
from app.constants.clientflags import ClientFlags
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.logging import Ansi
from app.logging import log
from app.objects.beatmap import Beatmap
from app.objects.beatmap import RankedStatus
from app.objects.beatmap import ensure_osu_file_is_available
from app.objects.score import Grade
from app.objects.score import Score
from app.objects.score import SubmissionStatus
from app.repositories import stats as stats_repo

REPLAYS_PATH = Path.cwd() / ".data/osr"
PENDING_PATH = REPLAYS_PATH / "pending"

# .osr timestamps are .NET ticks (100 ns since 0001-01-01)
_UNIX_EPOCH_TICKS = 621_355_968_000_000_000


class ApprovalError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _read_uleb128(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _read_string(data: bytes, pos: int) -> int:
    """Skip an osu! string, return the position after it."""
    flag = data[pos]
    pos += 1
    if flag == 0x0B:
        length, pos = _read_uleb128(data, pos)
        pos += length
    return pos


def parse_osr_times(data: bytes) -> tuple[datetime | None, int]:
    """(time the replay was played, length of the play in ms) from an .osr file."""
    try:
        pos = 1 + 4  # mode, version
        for _ in range(3):  # map md5, player name, replay md5
            pos = _read_string(data, pos)
        pos += 6 * 2 + 4 + 2 + 1 + 4  # hit counts, score, combo, perfect, mods
        pos = _read_string(data, pos)  # life bar
        (ticks,) = struct.unpack_from("<q", data, pos)
        pos += 8
        (replay_len,) = struct.unpack_from("<i", data, pos)
        pos += 4
    except (IndexError, struct.error):
        return None, 0

    played_at: datetime | None = None
    try:
        # the client writes UTC ticks; the database keeps local server time like submit does
        played_at = datetime.fromtimestamp((ticks - _UNIX_EPOCH_TICKS) / 10_000_000)
        if played_at > datetime.now() or played_at.year < 2007:
            played_at = None
    except (OverflowError, OSError, ValueError):
        played_at = None

    length_ms = 0
    if replay_len > 0:
        try:
            frames = lzma.decompress(data[pos : pos + replay_len], format=lzma.FORMAT_ALONE)
            for frame in frames.decode(errors="ignore").split(","):
                parts = frame.split("|")
                if len(parts) == 4 and parts[0] != "-12345":
                    w = int(parts[0])
                    if w > 0:
                        length_ms += w
        except Exception:
            length_ms = 0

    return played_at, length_ms


def _pending_file(osr_path: str) -> Path:
    """The pending .osr inside bancho's .data folder (Shiina stores its host path)."""
    return PENDING_PATH / Path(osr_path).name


async def approve_pending_replay(pending_id: int) -> dict[str, Any]:
    from app.api.domains.osu import _fmt_mods_for_announce
    from app.api.domains.osu import _is_ranked_for_first_places
    from app.api.domains.osu import pp_to_db
    from app.api.domains.osu import pp_to_float

    db = app.state.services.database

    row = await db.fetch_one(
        "SELECT * FROM pending_replays WHERE id = :id AND status = 0",
        {"id": pending_id},
    )
    if row is None:
        raise ApprovalError("not_found", "No pending replay with this id.", 404)

    player = await app.state.sessions.players.from_cache_or_sql(id=row["userid"])
    if player is None:
        raise ApprovalError("no_player", "The player does not exist.", 404)

    bmap = await Beatmap.from_md5(row["map_md5"])
    if bmap is None:
        raise ApprovalError("no_map", "The beatmap does not exist on the server.", 404)

    osr_file = _pending_file(row["osr_path"])
    if not osr_file.is_file():
        raise ApprovalError("no_replay", f"Replay file {osr_file.name} not found.", 404)
    osr_bytes = osr_file.read_bytes()
    played_at, length_ms = parse_osr_times(osr_bytes)

    mods = Mods(int(row["mods"]))
    mode_vn = int(row["mode"])
    if mode_vn == 3:  # no relax/autopilot leaderboards in mania
        mode = GameMode(3)
    else:
        mode = GameMode.from_params(mode_vn, mods)

    score = Score()
    score.bmap = bmap
    score.player = player
    score.mode = mode
    score.mods = mods
    score.score = int(row["score"])
    score.max_combo = int(row["max_combo"])
    score.n300 = int(row["n300"])
    score.n100 = int(row["n100"])
    score.n50 = int(row["n50"])
    score.nmiss = int(row["nmiss"])
    score.ngeki = int(row["ngeki"])
    score.nkatu = int(row["nkatu"])
    score.perfect = bool(row["perfect"])
    score.grade = Grade.from_str(row["grade"] or "N")
    score.passed = True
    score.client_checksum = row["replay_md5"]
    score.client_flags = ClientFlags(0)
    score.server_time = played_at or datetime.now()
    score.client_time = score.server_time
    score.time_elapsed = length_ms
    score.pp, score.sr = 0.0, 0.0
    score.status = SubmissionStatus.SUBMITTED

    # stats of an offline player are not in memory
    if mode not in player.stats:
        await player.stats_from_sql_full()

    async with app.state.score_submission_locks[score.client_checksum]:
        if await db.fetch_one(
            "SELECT 1 FROM scores WHERE online_checksum = :checksum",
            {"checksum": score.client_checksum},
        ):
            raise ApprovalError("duplicate", "This score already exists on the server.", 409)

        score.acc = score.calculate_accuracy()

        if await ensure_osu_file_is_available(bmap.id, expected_md5=bmap.md5):
            try:
                score.pp, score.sr = score.calculate_performance(bmap.id)
            except Exception as e:
                log(f"[replay-approve] pp calc failed for map {bmap.id}: {e}", Ansi.LYELLOW)
                score.pp, score.sr = 0.0, 0.0
        score.pp = pp_to_float(score.pp)

        await score.calculate_status()
        if bmap.status != RankedStatus.Pending:
            score.rank = await score.calculate_placement()

        # who holds #1 before this score is saved (for the bell of the player who loses it)
        previous_first = None
        if score.leaderboard_best and score.rank == 1 and not player.restricted and _is_ranked_for_first_places(bmap.status):
            from app.usecases.notifications import current_first_place
            previous_first = await current_first_place(bmap.md5, int(score.mode))

        params = {
            "map_md5": bmap.md5,
            "score": score.score,
            "pp": pp_to_db(score.pp),
            "acc": score.acc,
            "max_combo": score.max_combo,
            "mods": score.mods,
            "n300": score.n300,
            "n100": score.n100,
            "n50": score.n50,
            "nmiss": score.nmiss,
            "ngeki": score.ngeki,
            "nkatu": score.nkatu,
            "grade": score.grade.name,
            "status": score.status,
            "mode": score.mode,
            "play_time": score.server_time,
            "time_elapsed": score.time_elapsed,
            "client_flags": score.client_flags,
            "user_id": player.id,
            "perfect": score.perfect,
            "checksum": score.client_checksum,
        }
        insert_sql = (
            "INSERT INTO scores "
            "VALUES (NULL, "
            ":map_md5, :score, :pp, :acc, "
            ":max_combo, :mods, :n300, :n100, "
            ":n50, :nmiss, :ngeki, :nkatu, "
            ":grade, :status, :mode, :play_time, "
            ":time_elapsed, :client_flags, :user_id, :perfect, "
            ":checksum, 0, 0)"
        )
        keys = {"map_md5": bmap.md5, "user_id": player.id, "mode": score.mode}

        # same rules as osuSubmitModularSelector: one BEST and at most one
        # SCORE_BEST per player, map and mode
        async with db.transaction():
            await db.fetch_one(
                "SELECT id FROM users WHERE id = :user_id FOR UPDATE",
                {"user_id": player.id},
            )
            if score.status == SubmissionStatus.BEST:
                await db.execute(
                    "UPDATE scores SET status = 1 "
                    "WHERE status IN (2, 3) AND map_md5 = :map_md5 "
                    "AND userid = :user_id AND mode = :mode",
                    keys,
                )
                old_score_best = await db.fetch_one(
                    "SELECT id, score FROM scores "
                    "WHERE map_md5 = :map_md5 AND userid = :user_id "
                    "AND mode = :mode AND status = 1 "
                    "ORDER BY score DESC LIMIT 1",
                    keys,
                )
                if old_score_best and old_score_best["score"] > score.score:
                    await db.execute(
                        "UPDATE scores SET status = 3 WHERE id = :id",
                        {"id": old_score_best["id"]},
                    )
            elif score.status == SubmissionStatus.SCORE_BEST:
                await db.execute(
                    "UPDATE scores SET status = 1 "
                    "WHERE status = 3 AND map_md5 = :map_md5 "
                    "AND userid = :user_id AND mode = :mode",
                    keys,
                )
            score.id = await db.execute(insert_sql, params)
            await db.execute(
                "UPDATE pending_replays SET status = 1, reviewed_at = NOW(), "
                "score_id = :score_id WHERE id = :id",
                {"score_id": score.id, "id": pending_id},
            )

    # replay file next to the in-game ones
    try:
        shutil.move(str(osr_file), str(REPLAYS_PATH / f"{score.id}.osr"))
    except OSError as e:
        log(f"[replay-approve] could not move {osr_file}: {e}", Ansi.LRED)

    await app.state.services.redis.publish("ex:submit", score.toJSON())

    """ Player stats, the same way as after an in-game submission """

    stats = player.stats[score.mode]
    prev_rank = stats.rank

    stats.playtime += score.time_elapsed // 1000
    stats.plays += 1
    stats.tscore += score.score
    stats.total_hits += score.n300 + score.n100 + score.n50
    if score.mode.as_vanilla in (1, 3):
        stats.total_hits += score.ngeki + score.nkatu

    updates: dict[str, Any] = {
        "plays": stats.plays,
        "playtime": stats.playtime,
        "tscore": stats.tscore,
        "total_hits": stats.total_hits,
    }

    if bmap.has_leaderboard:
        if score.max_combo > stats.max_combo:
            stats.max_combo = score.max_combo
            updates["max_combo"] = stats.max_combo

        if bmap.awards_ranked_pp and score.status == SubmissionStatus.BEST:
            additional_rscore = score.score
            if score.prev_best:
                additional_rscore -= score.prev_best.score
                if score.grade != score.prev_best.grade:
                    if score.grade >= Grade.A:
                        stats.grades[score.grade] += 1
                        updates[format(score.grade, "stats_column")] = stats.grades[score.grade]
                    if score.prev_best.grade >= Grade.A:
                        stats.grades[score.prev_best.grade] -= 1
                        updates[format(score.prev_best.grade, "stats_column")] = stats.grades[
                            score.prev_best.grade
                        ]
            elif score.grade >= Grade.A:
                stats.grades[score.grade] += 1
                updates[format(score.grade, "stats_column")] = stats.grades[score.grade]

            stats.rscore += additional_rscore
            updates["rscore"] = stats.rscore

            best_scores = await db.fetch_all(
                "SELECT s.pp, s.acc FROM scores s "
                "INNER JOIN maps m ON s.map_md5 = m.md5 "
                "WHERE s.userid = :user_id AND s.mode = :mode "
                "AND s.status = 2 AND m.status IN (2, 3) "
                "ORDER BY s.pp DESC",
                {"user_id": player.id, "mode": score.mode},
            )
            weighted_acc = sum(float(r["acc"]) * 0.95**i for i, r in enumerate(best_scores))
            bonus_acc = 100.0 / (20 * (1 - 0.95 ** len(best_scores)))
            stats.acc = (weighted_acc * bonus_acc) / 100
            updates["acc"] = stats.acc

            weighted_pp = sum(pp_to_float(r["pp"]) * 0.95**i for i, r in enumerate(best_scores))
            bonus_pp = 416.6667 * (1 - 0.9994 ** len(best_scores))
            stats.pp = round(weighted_pp + bonus_pp)
            updates["pp"] = stats.pp

            stats.rank = await player.update_rank(score.mode)

    await stats_repo.partial_update(
        player.id,
        score.mode.value,
        plays=updates.get("plays", UNSET),
        playtime=updates.get("playtime", UNSET),
        tscore=updates.get("tscore", UNSET),
        total_hits=updates.get("total_hits", UNSET),
        max_combo=updates.get("max_combo", UNSET),
        xh_count=updates.get("xh_count", UNSET),
        x_count=updates.get("x_count", UNSET),
        sh_count=updates.get("sh_count", UNSET),
        s_count=updates.get("s_count", UNSET),
        a_count=updates.get("a_count", UNSET),
        rscore=updates.get("rscore", UNSET),
        acc=updates.get("acc", UNSET),
        pp=updates.get("pp", UNSET),
    )

    """ Messages """

    performance = f"{score.pp:,.2f}pp"
    if bmap.status == RankedStatus.Loved and score.mode < GameMode.RELAX_OSU:
        performance = f"{score.score:,} score"

    if player.is_online:
        text = f"Your replay on {bmap.full_name} was accepted"
        if score.leaderboard_best and bmap.has_leaderboard and score.rank:
            text += f": #{score.rank} ({performance})"
        player.enqueue(app.packets.notification(text + "."))
        if not player.restricted:
            app.state.sessions.players.enqueue(app.packets.user_stats(player))

    if (
        score.leaderboard_best
        and bmap.has_leaderboard
        and score.rank == 1
        and not player.restricted
        and _is_ranked_for_first_places(bmap.status)
    ):
        announce_chan = app.state.sessions.channels.get_by_name("#announce")
        if announce_chan is not None:
            ann = [
                f"\x01ACTION achieved #1 on {bmap.embed}",
                f"with {score.acc:.2f}% for {performance} (uploaded replay).",
            ]
            if score.mods:
                ann.insert(1, f"+{_fmt_mods_for_announce(score.mods)}")
            announce_chan.send(" ".join(ann), sender=player, to_self=True)

    from app.usecases.notifications import notify
    from app.usecases.notifications import notify_first_place_lost
    await notify(player.id, "replay_accepted", {"map": bmap.full_name, "pp": round(score.pp, 2)}, f"/scores/{score.id}")
    await notify_first_place_lost(previous_first, player.id, player.name, bmap)

    log(
        f"[replay-approve] pending {pending_id} -> score {score.id} "
        f"({player.name}, {score.mode!r}, {score.pp:.2f}pp, status {score.status!r}, "
        f"rank {stats.rank} was {prev_rank})",
        Ansi.LGREEN,
    )

    return {
        "status": "success",
        "score_id": score.id,
        "pp": round(score.pp, 3),
        "acc": round(score.acc, 2),
        "mode": int(score.mode),
        "submission_status": int(score.status),
        "map_rank": score.rank,
        "player_rank": stats.rank,
    }
