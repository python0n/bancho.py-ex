from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime as datetime
from datetime import timedelta as timedelta
from enum import IntEnum
from enum import unique
from typing import TYPE_CHECKING
from typing import TypedDict

import app.packets
import app.settings
import app.state
from app.constants import regexes
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.objects.beatmap import Beatmap
from app.repositories.tourney_pools import TourneyPool
from app.utils import escape_enum
from app.utils import pymysql_encode

if TYPE_CHECKING:
    from asyncio import TimerHandle

    from app.objects.channel import Channel
    from app.objects.player import Player


MAX_MATCH_NAME_LENGTH = 50

# osu! wiki: rooms made with !mp make close 30 minutes after becoming empty
EMPTY_MATCH_TIMEOUT = 30 * 60


@unique
@pymysql_encode(escape_enum)
class SlotStatus(IntEnum):
    open = 1
    locked = 2
    not_ready = 4
    ready = 8
    no_map = 16
    playing = 32
    complete = 64
    quit = 128

    # has_player = not_ready | ready | no_map | playing | complete


@unique
@pymysql_encode(escape_enum)
class MatchTeams(IntEnum):
    neutral = 0
    blue = 1
    red = 2


"""
# implemented by osu! and send between client/server,
# quite frequently even, but seems useless??
@unique
@pymysql_encode(escape_enum)
class MatchTypes(IntEnum):
    standard  = 0
    powerplay = 1 # literally no idea what this is for
"""


@unique
@pymysql_encode(escape_enum)
class MatchWinConditions(IntEnum):
    score = 0
    accuracy = 1
    combo = 2
    scorev2 = 3


@unique
@pymysql_encode(escape_enum)
class MatchTeamTypes(IntEnum):
    head_to_head = 0
    tag_coop = 1
    team_vs = 2
    tag_team_vs = 3


class Slot:
    """An individual player slot in an osu! multiplayer match."""

    def __init__(self) -> None:
        self.player: Player | None = None
        self.status = SlotStatus.open
        self.team = MatchTeams.neutral
        self.mods = Mods.NOMOD
        self.loaded = False
        self.skipped = False
        self.failed = False  # MATCH_FAILED without NF; cleared again on revive
        self.last_score_frame: dict | None = None

    def empty(self) -> bool:
        return self.player is None

    def copy_from(self, other: Slot) -> None:
        self.player = other.player
        self.status = other.status
        self.team = other.team
        self.mods = other.mods

    def reset(self, new_status: SlotStatus = SlotStatus.open) -> None:
        self.player = None
        self.status = new_status
        self.team = MatchTeams.neutral
        self.mods = Mods.NOMOD
        self.loaded = False
        self.skipped = False
        self.failed = False
        self.last_score_frame = None


class StartingTimers(TypedDict):
    start: TimerHandle
    alerts: list[TimerHandle]
    time: float


class Match:
    """\
    An osu! multiplayer match.

    Possibly confusing attributes
    -----------
    _refs: set[`Player`]
        A set of players who have access to mp commands in the match.
        These can be used with the !mp <addref/rmref/listref> commands.

    slots: list[`Slot`]
        A list of 16 `Slot` objects representing the match's slots.

    starting: dict[str, `TimerHandle`] | None
        Used when the match is started with !mp start <seconds>.
        It stores both the starting timer, and the chat alert timers.

    seed: `int`
        The seed used for osu!mania's random mod.

    use_pp_scoring: `bool`
        Whether pp should be used as a win condition override during scrims.
    """

    def __init__(
        self,
        id: int,
        name: str,
        password: str,
        has_public_history: bool,
        map_name: str,
        map_id: int,
        map_md5: str,
        host_id: int,
        mode: GameMode,
        mods: Mods,
        win_condition: MatchWinConditions,
        team_type: MatchTeamTypes,
        freemods: bool,
        seed: int,
        chat_channel: Channel,
        seq_id: int = 0,
    ) -> None:
        self.id = id
        self.seq_id = seq_id  # ever-incrementing ID for IRC channel names
        self.name = name
        self.passwd = password
        self.has_public_history = has_public_history

        self.host_id = host_id
        self.referees: set[Player] = set()

        self.map_id = map_id
        self.map_md5 = map_md5
        self.map_name = map_name
        self.prev_map_id = 0  # previously chosen map

        self.mods = mods
        self.mode = mode
        self.freemods = freemods

        self.chat = chat_channel
        self.slots = [Slot() for _ in range(16)]

        # self.type = MatchTypes.standard
        self.team_type = team_type
        self.win_condition = win_condition

        self.in_progress = False
        self.starting: StartingTimers | None = None
        self.seed = seed  # used for mania random mod

        self.tourney_pool: TourneyPool | None = None

        # scrimmage stuff
        self.is_scrimming = False
        self.match_points: dict[MatchTeams | Player, int] = defaultdict(int)
        self.bans: set[tuple[Mods, int]] = set()
        self.winners: list[Player | MatchTeams | None] = []  # none for tie
        self.winning_pts = 0
        self.use_pp_scoring = False  # only for scrims

        self.tourney_clients: set[int] = set()  # player ids

        self.web_id: int | None = None  # id in mp_matches (match history page)
        self.current_game_id: int | None = None  # id in mp_match_games
        self._game_insert: asyncio.Task | None = None
        self._all_loaded_sent = False
        self._skip_sent = False

        # made with !mp make: stays open while empty and closes after
        # EMPTY_MATCH_TIMEOUT, like official bancho
        self.ref_made = False
        self._empty_timer: TimerHandle | None = None

    @property
    def host(self) -> Player | None:
        """The match host, or None (after !mp clearhost or if they went offline)."""
        if self.host_id < 0:
            return None
        return app.state.sessions.players.get(id=self.host_id)

    @property
    def history_url(self) -> str:
        """Match history page on our website (osu-web has /mp/<id>)."""
        return f"https://osu.{app.settings.DOMAIN}/matches/{self.web_id or self.id}"

    @property
    def url(self) -> str:
        """The match's invitation url."""
        return f"osump://{self.id}/{self.passwd}"

    @property
    def map_url(self) -> str:
        """The osu! beatmap url for `self`'s map."""
        return f"https://osu.{app.settings.DOMAIN}/b/{self.map_id}"

    @property
    def embed(self) -> str:
        """An osu! chat embed for `self`."""
        return f"[{self.url} {self.name}]"

    @property
    def map_embed(self) -> str:
        """An osu! chat embed for `self`'s map."""
        return f"[{self.map_url} {self.map_name}]"

    @property
    def refs(self) -> set[Player]:
        """Return all players with referee permissions."""
        host = self.host
        return self.referees | ({host} if host is not None else set())

    def __repr__(self) -> str:
        return f"<{self.name} ({self.id})>"

    def get_slot(self, player: Player) -> Slot | None:
        """Return the slot containing a given player."""
        for s in self.slots:
            if player is s.player:
                return s

        return None

    def get_slot_id(self, player: Player) -> int | None:
        """Return the slot index containing a given player."""
        for idx, s in enumerate(self.slots):
            if player is s.player:
                return idx

        return None

    def get_free(self) -> int | None:
        """Return the first unoccupied slot in multi, if any."""
        for idx, s in enumerate(self.slots):
            if s.status == SlotStatus.open:
                return idx

        return None

    def get_host_slot(self) -> Slot | None:
        """Return the slot containing the host."""
        host = self.host
        if host is None:
            return None
        for s in self.slots:
            if s.player is host:
                return s

        return None

    def copy(self, m: Match) -> None:
        """Fully copy the data of another match obj."""
        self.map_id = m.map_id
        self.map_md5 = m.map_md5
        self.map_name = m.map_name
        self.freemods = m.freemods
        self.mode = m.mode
        self.team_type = m.team_type
        self.win_condition = m.win_condition
        self.mods = m.mods
        self.name = m.name

    def enqueue(
        self,
        data: bytes,
        lobby: bool = True,
        immune: Sequence[int] = [],
    ) -> None:
        """Add data to be sent to all clients in the match."""
        self.chat.enqueue(data, immune)

        lchan = app.state.sessions.channels.get_by_name("#lobby")
        if lobby and lchan and lchan.players:
            lchan.enqueue(data)


    def enqueue_state(self, lobby: bool = True) -> None:
        """Enqueue `self`'s state to players in the match & lobby."""
        # TODO: hmm this is pretty bad, writes twice

        # send password only to users currently in the match.
        self.chat.enqueue(app.packets.update_match(self, send_pw=True))

        lchan = app.state.sessions.channels.get_by_name("#lobby")
        if lobby and lchan and lchan.players:
            lchan.enqueue(app.packets.update_match(self, send_pw=False))

    def unready_players(self, expected: SlotStatus = SlotStatus.ready) -> None:
        """Unready any players in the `expected` state."""
        for s in self.slots:
            if s.status == expected:
                s.status = SlotStatus.not_ready

    def reset_players_loaded_status(self) -> None:
        """Reset all players' loaded status."""
        for s in self.slots:
            s.loaded = False
            s.skipped = False

    @property
    def game_mods(self) -> Mods:
        """Mods of the game itself; the client adds ScoreV2 on its own when
        that's the win condition, so it is stored too."""
        mods = self.mods
        if self.win_condition == MatchWinConditions.scorev2:
            mods |= Mods.SCOREV2
        return mods

    def start(self) -> None:
        """Start the match for all ready players with the map."""
        no_map: list[int] = []

        for s in self.slots:
            s.loaded = False
            s.skipped = False
            s.failed = False
            s.last_score_frame = None
            # start each player who has the map.
            if s.player is not None:
                if s.status != SlotStatus.no_map:
                    s.status = SlotStatus.playing
                else:
                    no_map.append(s.player.id)

        self.in_progress = True
        self._all_loaded_sent = False
        self._skip_sent = False
        self.current_game_id = None
        # zapis gry do bazy (start z klienta ORAZ !mp start)
        if self.web_id:
            async def _record_game(m=self) -> None:
                try:
                    m.current_game_id = await app.state.services.database.execute(
                        "INSERT INTO mp_match_games (match_id, map_id, map_md5, mode, scoring_type, team_type, mods, started_at) "
                        "VALUES (:match_id, :map_id, :map_md5, :mode, :scoring_type, :team_type, :mods, NOW())",
                        {
                            "match_id": m.web_id,
                            "map_id": m.map_id,
                            "map_md5": m.map_md5,
                            "mode": int(m.mode),
                            "scoring_type": int(m.win_condition),
                            "team_type": int(m.team_type),
                            "mods": int(m.game_mods),
                        },
                    )
                except Exception as exc:
                    print(f"[mp] Failed to insert mp_match_games: {exc}", flush=True)
            self._game_insert = asyncio.create_task(_record_game())
        self.enqueue(app.packets.match_start(self), immune=no_map, lobby=False)
        self.enqueue_state()

    # ------------------------------------------------------------ game flow
    # Called after every change that can end a phase (load, skip, complete),
    # including a player leaving or disconnecting, so the match never hangs.

    def check_all_loaded(self) -> None:
        if not self.in_progress or self._all_loaded_sent:
            return
        if any(s.status == SlotStatus.playing and not s.loaded for s in self.slots):
            return
        self._all_loaded_sent = True
        self.enqueue(app.packets.match_all_players_loaded(), lobby=False)

    def check_all_skipped(self) -> None:
        if not self.in_progress or self._skip_sent:
            return
        playing = [s for s in self.slots if s.status == SlotStatus.playing]
        if not playing or any(not s.skipped for s in playing):
            return
        self._skip_sent = True
        self.enqueue(app.packets.match_skip(), lobby=False)

    def check_complete(self) -> None:
        """Finish the game once nobody is still playing."""
        if not self.in_progress:
            return
        if any(s.status == SlotStatus.playing for s in self.slots):
            return

        was_playing = [
            s for s in self.slots
            if s.player is not None and s.status == SlotStatus.complete
        ]
        not_playing = [
            s.player.id for s in self.slots
            if s.player is not None and s.status != SlotStatus.complete
        ]

        results = [self._result_of(s) for s in was_playing]
        if results:
            self._announce_results(results)

        self.unready_players(expected=SlotStatus.complete)
        self.reset_players_loaded_status()
        self.in_progress = False

        self.enqueue(app.packets.match_complete(), lobby=False, immune=not_playing)
        self.enqueue_state()

        if self.web_id:
            asyncio.create_task(self._save_game(results))

        if self.is_scrimming and was_playing:
            asyncio.create_task(self.update_matchpoints(was_playing))

    def abort(self) -> None:
        self.unready_players(expected=SlotStatus.playing)
        self.reset_players_loaded_status()
        self.in_progress = False
        self.enqueue(app.packets.match_abort(), lobby=False)
        self.enqueue_state()
        if self.web_id:
            asyncio.create_task(self._end_game())

    def _result_of(self, slot: Slot) -> dict:
        assert slot.player is not None
        sf = slot.last_score_frame or {}
        mods = slot.mods | self.game_mods if self.freemods else self.game_mods
        if sf.get("sv2"):
            mods |= Mods.SCOREV2
        n300, n100, n50 = sf.get("n300", 0), sf.get("n100", 0), sf.get("n50", 0)
        ngeki, nkatu, nmiss = sf.get("ngeki", 0), sf.get("nkatu", 0), sf.get("nmiss", 0)
        acc = calculate_accuracy(self.mode.as_vanilla, n300, n100, n50, ngeki, nkatu, nmiss)
        passed = not slot.failed
        return {
            "player": slot.player,
            "team": slot.team,
            "has_frame": bool(sf),
            "score": sf.get("score", 0),
            "max_combo": sf.get("max_combo", 0),
            "n300": n300, "n100": n100, "n50": n50,
            "ngeki": ngeki, "nkatu": nkatu, "nmiss": nmiss,
            "acc": acc,
            "mods": int(mods),
            "passed": passed,
            "grade": calculate_grade(self.mode.as_vanilla, mods, n300, n100, n50, ngeki, nkatu, nmiss, passed),
        }

    def _announce_results(self, results: list[dict]) -> None:
        key = {
            MatchWinConditions.accuracy: lambda r: (r["acc"], r["score"]),
            MatchWinConditions.combo: lambda r: (r["max_combo"], r["score"]),
        }.get(self.win_condition, lambda r: (r["score"],))
        self.chat.send_bot("Match ended!")
        for rank, r in enumerate(sorted(results, key=key, reverse=True), 1):
            self.chat.send_bot(
                f"#{rank} {r['player'].name}: {r['score']:,} ({r['acc']:.2f}%) x{r['max_combo']}",
            )

    async def _game_id(self) -> int | None:
        if self._game_insert is not None:
            try:
                await self._game_insert
            except Exception:
                pass
        return self.current_game_id

    async def _end_game(self) -> int | None:
        game_id = await self._game_id()
        if game_id:
            try:
                await app.state.services.database.execute(
                    "UPDATE mp_match_games SET ended_at = NOW() WHERE id = :id",
                    {"id": game_id},
                )
            except Exception as exc:
                print(f"[mp] Failed to end game {game_id}: {exc}", flush=True)
        return game_id

    async def _save_game(self, results: list[dict]) -> None:
        """Store the game the way official bancho does: straight from the
        players' last score frames, right when the game ends."""
        game_id = await self._end_game()
        if not game_id:
            return
        for r in results:
            if not r["has_frame"]:
                continue
            try:
                await app.state.services.database.execute(
                    "INSERT INTO mp_match_scores "
                    "(game_id, user_id, score, acc, max_combo, mods, n300, n100, n50, nmiss, ngeki, nkatu, grade, passed, team) "
                    "VALUES (:game_id, :user_id, :score, :acc, :max_combo, :mods, :n300, :n100, :n50, :nmiss, :ngeki, :nkatu, :grade, :passed, :team)",
                    {
                        "game_id": game_id,
                        "user_id": r["player"].id,
                        "score": r["score"],
                        "acc": round(r["acc"], 3),
                        "max_combo": r["max_combo"],
                        "mods": r["mods"],
                        "n300": r["n300"],
                        "n100": r["n100"],
                        "n50": r["n50"],
                        "nmiss": r["nmiss"],
                        "ngeki": r["ngeki"],
                        "nkatu": r["nkatu"],
                        "grade": r["grade"],
                        "passed": 1 if r["passed"] else 0,
                        "team": int(r["team"]),
                    },
                )
            except Exception as exc:
                print(f"[mp] Failed to save score of {r['player']}: {exc}", flush=True)

    # ------------------------------------------------------------ empty / close

    def schedule_empty_close(self) -> None:
        self.cancel_empty_close()
        self._empty_timer = app.state.loop.call_later(
            EMPTY_MATCH_TIMEOUT, self._close_if_empty,
        )

    def cancel_empty_close(self) -> None:
        if self._empty_timer is not None:
            self._empty_timer.cancel()
            self._empty_timer = None

    def _close_if_empty(self) -> None:
        self._empty_timer = None
        if all(s.empty() for s in self.slots):
            self.chat.send_bot("Closing the empty room.")
            self.close()

    def close(self) -> None:
        """Close the match: remove everybody and dispose it."""
        self.cancel_empty_close()
        if self.starting is not None:
            self.starting["start"].cancel()
            for alert in self.starting["alerts"]:
                alert.cancel()
            self.starting = None

        slotted = [s.player for s in self.slots if s.player is not None]
        for s in self.slots:
            if s.player is not None:
                s.reset()
        # empty match state makes the osu! client leave the room ui
        self.enqueue_state(lobby=False)

        for p in list(self.chat.players):
            p.leave_channel(self.chat)
            if p.match is self:
                p.match = None
        for p in slotted:
            if p.match is self:
                p.match = None

        if self.web_id:
            asyncio.create_task(
                app.state.services.database.execute(
                    "UPDATE mp_matches SET ended_at = NOW() WHERE id = :id",
                    {"id": self.web_id},
                ),
            )

        app.state.sessions.matches.remove(self)
        lobby = app.state.sessions.channels.get_by_name("#lobby")
        if lobby:
            lobby.enqueue(app.packets.dispose_match(self.id))

    def reset_scrim(self) -> None:
        """Reset the current scrim's winning points & bans."""
        self.match_points.clear()
        self.winners.clear()
        self.bans.clear()

    async def await_submissions(
        self,
        was_playing: Sequence[Slot],
    ) -> tuple[dict[MatchTeams | Player, int], Sequence[Player]]:
        """Await score submissions from all players in completed state."""
        scores: dict[MatchTeams | Player, int] = defaultdict(int)
        didnt_submit: list[Player] = []
        time_waited = 0.0  # allow up to 10s (total, not per player)

        ffa = self.team_type in (MatchTeamTypes.head_to_head, MatchTeamTypes.tag_coop)

        if self.use_pp_scoring:
            win_cond = "pp"
        else:
            win_cond = ("score", "acc", "max_combo", "score")[self.win_condition]

        bmap = await Beatmap.from_md5(self.map_md5)

        if not bmap:
            # map isn't submitted
            return {}, ()

        for s in was_playing:
            # continue trying to fetch each player's
            # scores until they've all been submitted.
            while True:
                assert s.player is not None
                rc_score = s.player.recent_score

                max_age = datetime.now() - timedelta(
                    seconds=bmap.total_length + time_waited + 0.5,
                )

                if (
                    rc_score
                    and rc_score.bmap
                    and rc_score.bmap.md5 == self.map_md5
                    and rc_score.server_time > max_age
                ):
                    # score found, add to our scores dict if != 0.
                    score: int = getattr(rc_score, win_cond)
                    if score:
                        key: MatchTeams | Player = s.player if ffa else s.team
                        scores[key] += score

                    break

                # wait 0.5s and try again
                await asyncio.sleep(0.5)
                time_waited += 0.5

                if time_waited > 10:
                    # inform the match this user didn't
                    # submit a score in time, and skip them.
                    didnt_submit.append(s.player)
                    break

        # all scores retrieved, update the match.
        return scores, didnt_submit

    async def update_matchpoints(self, was_playing: Sequence[Slot]) -> None:
        """\
        Determine the winner from `scores`, increment & inform players.

        This automatically works with the match settings (such as
        win condition, teams, & co-op) to determine the appropriate
        winner, and will use any team names included in the match name,
        along with the match name (fmt: OWC2020: (Team1) vs. (Team2)).

        For the examples, we'll use accuracy as a win condition.

        Teams, match title: `OWC2015: (United States) vs. (China)`.
          United States takes the point! (293.32% vs 292.12%)
          Total Score: United States | 7 - 2 | China
          United States takes the match, finishing OWC2015 with a score of 7 - 2!

        FFA, the top <=3 players will be listed for the total score.
          Justice takes the point! (94.32% [Match avg. 91.22%])
          Total Score: Justice - 3 | cmyui - 2 | FrostiDrinks - 2
          Justice takes the match, finishing with a score of 4 - 2!
        """

        scores, didnt_submit = await self.await_submissions(was_playing)

        for player in didnt_submit:
            self.chat.send_bot(f"{player} didn't submit a score (timeout: 10s).")

        if not scores:
            self.chat.send_bot("Scores could not be calculated.")
            return None

        ffa = self.team_type in (
            MatchTeamTypes.head_to_head,
            MatchTeamTypes.tag_coop,
        )

        # all scores are equal, it was a tie.
        if len(scores) != 1 and len(set(scores.values())) == 1:
            self.winners.append(None)
            self.chat.send_bot("The point has ended in a tie!")
            return None

        # Find the winner & increment their matchpoints.
        winner: Player | MatchTeams = max(scores, key=lambda k: scores[k])
        self.winners.append(winner)
        self.match_points[winner] += 1

        msg: list[str] = []

        def add_suffix(score: int | float) -> str | int | float:
            if self.use_pp_scoring:
                return f"{score:.2f}pp"
            elif self.win_condition == MatchWinConditions.accuracy:
                return f"{score:.2f}%"
            elif self.win_condition == MatchWinConditions.combo:
                return f"{score}x"
            else:
                return str(score)

        if ffa:
            from app.objects.player import Player

            assert isinstance(winner, Player)

            msg.append(
                f"{winner.name} takes the point! ({add_suffix(scores[winner])} "
                f"[Match avg. {add_suffix(sum(scores.values()) / len(scores))}])",
            )

            wmp = self.match_points[winner]

            # check if match point #1 has enough points to win.
            if self.winning_pts and wmp == self.winning_pts:
                # we have a champion, announce & reset our match.
                self.is_scrimming = False
                self.reset_scrim()
                self.bans.clear()

                m = f"{winner.name} takes the match! Congratulations!"
            else:
                # no winner, just announce the match points so far.
                # for ffa, we'll only announce the top <=3 players.
                m_points = sorted(self.match_points.items(), key=lambda x: x[1])
                m = f"Total Score: {' | '.join([f'{k.name} - {v}' for k, v in m_points])}"

            msg.append(m)
            del m

        else:  # teams
            assert isinstance(winner, MatchTeams)

            r_match = regexes.TOURNEY_MATCHNAME.match(self.name)
            if r_match:
                match_name = r_match["name"]
                team_names = {
                    MatchTeams.blue: r_match["T1"],
                    MatchTeams.red: r_match["T2"],
                }
            else:
                match_name = self.name
                team_names = {MatchTeams.blue: "Blue", MatchTeams.red: "Red"}

            # teams are binary, so we have a loser.
            if winner is MatchTeams.blue:
                loser = MatchTeams.red
            else:
                loser = MatchTeams.blue

            # from match name if available, else blue/red.
            wname = team_names[winner]
            lname = team_names[loser]

            # scores from the recent play
            # (according to win condition)
            ws = add_suffix(scores[winner])
            ls = add_suffix(scores[loser])

            # total win/loss score in the match.
            wmp = self.match_points[winner]
            lmp = self.match_points[loser]

            # announce the score for the most recent play.
            msg.append(f"{wname} takes the point! ({ws} vs. {ls})")

            # check if the winner has enough match points to win the match.
            if self.winning_pts and wmp == self.winning_pts:
                # we have a champion, announce & reset our match.
                self.is_scrimming = False
                self.reset_scrim()

                msg.append(
                    f"{wname} takes the match, finishing {match_name} "
                    f"with a score of {wmp} - {lmp}! Congratulations!",
                )
            else:
                # no winner, just announce the match points so far.
                msg.append(f"Total Score: {wname} | {wmp} - {lmp} | {lname}")

        if didnt_submit:
            self.chat.send_bot(
                "If you'd like to perform a rematch, "
                "please use the `!mp rematch` command.",
            )

        for line in msg:
            self.chat.send_bot(line)


def calculate_accuracy(mode: int, n300: int, n100: int, n50: int,
                       ngeki: int, nkatu: int, nmiss: int) -> float:
    """Accuracy like osu-web (Scoreable::accuracy) for vanilla modes 0-3."""
    if mode == 1:  # taiko
        total = n300 + n100 + nmiss
        return 100.0 * (n300 + n100 * 0.5) / total if total else 0.0
    if mode == 2:  # catch
        total = n300 + n100 + n50 + nkatu + nmiss
        return 100.0 * (n300 + n100 + n50) / total if total else 0.0
    if mode == 3:  # mania
        total = n300 + n100 + n50 + ngeki + nkatu + nmiss
        if not total:
            return 0.0
        return 100.0 * (300 * (n300 + ngeki) + 200 * nkatu + 100 * n100 + 50 * n50) / (300 * total)
    total = n300 + n100 + n50 + nmiss
    return 100.0 * (300 * n300 + 100 * n100 + 50 * n50) / (300 * total) if total else 0.0


def calculate_grade(mode: int, mods: Mods, n300: int, n100: int, n50: int,
                    ngeki: int, nkatu: int, nmiss: int, passed: bool) -> str:
    """Grade like osu-web (game_scores has no grade column, it's computed)."""
    if not passed:
        return "F"
    hidden = bool(mods & (Mods.HIDDEN | Mods.FLASHLIGHT | Mods.FADEIN))
    acc = calculate_accuracy(mode, n300, n100, n50, ngeki, nkatu, nmiss)

    if mode == 2:  # catch
        if acc >= 100:
            grade = "X"
        elif acc > 98:
            grade = "S"
        elif acc > 94:
            grade = "A"
        elif acc > 90:
            grade = "B"
        elif acc > 85:
            grade = "C"
        else:
            grade = "D"
    elif mode == 3:  # mania
        if acc >= 100:
            grade = "X"
        elif acc > 95:
            grade = "S"
        elif acc > 90:
            grade = "A"
        elif acc > 80:
            grade = "B"
        elif acc > 70:
            grade = "C"
        else:
            grade = "D"
    else:  # osu!, taiko
        total = n300 + n100 + n50 + nmiss if mode == 0 else n300 + n100 + nmiss
        r300 = n300 / total if total else 0.0
        r50 = n50 / total if total and mode == 0 else 0.0
        if total and n300 == total:
            grade = "X"
        elif r300 > 0.9 and r50 <= 0.01 and nmiss == 0:
            grade = "S"
        elif (r300 > 0.8 and nmiss == 0) or r300 > 0.9:
            grade = "A"
        elif (r300 > 0.7 and nmiss == 0) or r300 > 0.8:
            grade = "B"
        elif r300 > 0.6:
            grade = "C"
        else:
            grade = "D"

    if hidden and grade in ("X", "S"):
        grade += "H"
    return grade
