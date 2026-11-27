import time
import app
from app.constants.gamemodes import GameMode
from app.constants.privileges import Privileges
from app.objects.beatmap import Beatmap, ensure_osu_file_is_available
from app.repositories import maps as maps_repo
from pytimeparse.timeparse import timeparse

MAX_SUPPORTER_SECONDS = 365 * 24 * 60 * 60

str_priv_dict = {
    "normal": Privileges.UNRESTRICTED,
    "verified": Privileges.VERIFIED,
    "whitelisted": Privileges.WHITELISTED,
    "supporter": Privileges.SUPPORTER,
    "premium": Privileges.PREMIUM,
    "alumni": Privileges.ALUMNI,
    "tournament": Privileges.TOURNEY_MANAGER,
    "nominator": Privileges.NOMINATOR,
    "mod": Privileges.MODERATOR,
    "admin": Privileges.ADMINISTRATOR,
    "developer": Privileges.DEVELOPER,
}

async def wipe_user(id: int, mode: GameMode) -> str:
    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    if not target:
        return "user not found"
    
    await app.state.services.database.execute("DELETE FROM scores WHERE userid = :user_id AND mode = :mode",
        {"user_id": id, "mode": mode},)
    
    await app.state.services.database.execute(
        """
        UPDATE stats 
        SET 
            pp = :pp, 
            acc = :acc, 
            tscore = :tscore, 
            rscore = :rscore, 
            plays = :plays, 
            playtime = :playtime, 
            max_combo = :max_combo, 
            replay_views = :replay_views, 
            total_hits = :total_hits,
            xh_count = :xh_count, 
            x_count = :x_count, 
            s_count = :s_count, 
            a_count = :a_count 
        WHERE id = :id AND mode = :mode
        """,
        {
            "pp": 0, 
            "acc": 0, 
            "tscore": 0, 
            "rscore": 0, 
            "plays": 0, 
            "playtime": 0, 
            "max_combo": 0, 
            "replay_views": 0, 
            "total_hits": 0,
            "xh_count": 0, 
            "x_count": 0, 
            "s_count": 0, 
            "a_count": 0, 
            "id": id, 
            "mode": mode
        }
    )

    user_info = await app.state.services.database.fetch_one(
        "SELECT country, priv FROM users WHERE id = :id",
        {"id": id},
    )

    if user_info is None:
        return "unknown user id"
    

    await app.state.services.redis.zrem(
        f"bancho:leaderboard:{mode}",
        str(id),
    )

    await app.state.services.redis.zrem(
        f"bancho:leaderboard:{mode}:{user_info['country']}",
        str(id),
    )

    return "success"

async def change_user_flag(id: int, flag: str) -> str:
    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    if not target:
        return "user not found"
    
    countryBefore = await app.state.services.database.fetch_one(
        "SELECT country FROM users WHERE id = :id",
        {"id": id},
    )

    await app.state.services.database.execute(
        "UPDATE users SET country = :country WHERE id = :user_id",
        {"country": flag.lower(), "user_id": id},
    )

    for mode in GameMode:
        modequery = await app.state.services.database.fetch_one(
            "SELECT pp FROM stats WHERE id = :id AND mode = :mode",
            {"id": id, "mode": mode},
        ) 

        key = f"bancho:leaderboard:{mode}:{countryBefore['country']}"
        if await app.state.services.redis.zscore(key, str(id)) is not None:
            await app.state.services.redis.zrem(key, str(id))

        if modequery:
            if modequery["pp"] == 0:
                continue
            newkey = f"bancho:leaderboard:{mode}:{flag.lower()}"
            await app.state.services.redis.zadd(newkey, {str(id): modequery["pp"]})
            
    return "success"

async def change_user_name(id: int, name: str) -> str:
    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    if not target:
        return "user not found"
    
    await app.state.services.database.execute(
        "UPDATE users SET name = :name WHERE id = :user_id",
        {"name": name, "user_id": id},
    )

    target.name = name

    if target.is_online:
        target.logout()

    return "success"

async def change_bm_status(beatmap_id: int, status: int, frozen: bool) -> str:
    beatmap = await Beatmap.from_bid(beatmap_id)

    if not beatmap:
        return "beatmap not found"

    osu_file_available = await ensure_osu_file_is_available(
        beatmap.id,
        expected_md5=beatmap.md5,
    )
    if not osu_file_available:
        return "osu file not found"
    

    await maps_repo.partial_update(beatmap.id, status=status, frozen=frozen)
    beatmap.status = status
    beatmap.frozen = frozen

    return "success"

async def restrict(id: int, userId: int, reason: str) -> str:

    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    user = await app.state.sessions.players.from_cache_or_sql(id=userId)
    if not target:
        return "user not found"
    if not user:
        return "admin not found"
    
    if target.priv & Privileges.STAFF and not user.priv & Privileges.DEVELOPER:
        return "Only developers can manage staff members."
    
    if target.restricted:
        return "User is already restricted!"
    
    await target.restrict(admin=user, reason=reason)

    if target.is_online:
        target.logout()

    return "success"

async def unrestrict(id: int, userId: int, reason: str) -> str:
    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    user = await app.state.sessions.players.from_cache_or_sql(id=userId)
    if not target:
        return "user not found"
    if not user:
        return "admin not found"
    
    if target.priv & Privileges.STAFF and not user.priv & Privileges.DEVELOPER:
        return "Only developers can manage staff members."
    
    if not target.restricted:
        return "User is not restricted!"
    
    await target.unrestrict(admin=user, reason=reason)

    if target.is_online:
        target.logout()
    
    return "success"

async def alert_all(message: str) -> str:
    app.state.sessions.players.enqueue(app.packets.notification(message))
    return "success"

async def givedonator(id: int, duration: any) -> str:
    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    if not target:
        return "user not found"
    
    timespan = timeparse(duration)
    if not timespan:
        return "Invalid timespan."
    
    if target.donor_end < time.time():
        timespan += time.time()
    else:
        timespan += target.donor_end

    # supporter lasts at most a year from now, also when added on top of a running one
    timespan = min(timespan, time.time() + MAX_SUPPORTER_SECONDS)

    target.donor_end = int(timespan)
    
    await app.state.services.database.execute(
        "UPDATE users SET donor_end = :end WHERE id = :user_id",
        {"end": timespan, "user_id": target.id},
    )

    await target.add_privs(Privileges.SUPPORTER)
    return "success"

from typing import List
from typing import Optional

# privileges only a developer may give or take: staff ranks and "normal"
# (taking "normal" away restricts the player without the restrict log)
DEVELOPER_ONLY = Privileges.STAFF | Privileges.UNRESTRICTED


async def _actor_is_developer(actor_id: Optional[int]) -> bool:
    if not actor_id:
        return False
    row = await app.state.services.database.fetch_one(
        "SELECT priv FROM users WHERE id = :id", {"id": actor_id},
    )
    return row is not None and bool(int(row["priv"]) & Privileges.DEVELOPER)


async def _change_privs(id: int, privs: List[str], actor_id: Optional[int], add: bool) -> str:
    bits = Privileges(0)

    for priv in privs:
        if priv not in str_priv_dict:
            return f"Invalid privilege: {priv}"
        bits |= str_priv_dict[priv]

    target = await app.state.sessions.players.from_cache_or_sql(id=id)
    if not target:
        return "user not found"

    if bits & Privileges.DONATOR != 0:
        return "use givedonor."

    # TSG-09: the hierarchy is checked here too, not only in Shiina's panel
    if bits & DEVELOPER_ONLY or target.priv & Privileges.STAFF:
        if not await _actor_is_developer(actor_id):
            return "Only developers can change staff privileges or staff members."

    if add:
        await target.add_privs(bits)
    else:
        await target.remove_privs(bits)
    return "success"


async def addpriv(id: int, privs: List[str], actor_id: Optional[int] = None) -> str:
    return await _change_privs(id, privs, actor_id, add=True)


async def removepriv(id: int, privs: List[str], actor_id: Optional[int] = None) -> str:
    return await _change_privs(id, privs, actor_id, add=False)
