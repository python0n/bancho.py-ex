"""Notifications for the bell on the website (Shiina reads the notifications table).

type             data                               link
replay_accepted  map, pp                            /scores/<id>
first_place_lost user, map                          /b/<id>
map_status       map, status (ranked/loved/...)     /b/<id>
(Shiina adds replay_rejected, map_comment and deletion_rejected itself)
"""

from __future__ import annotations

import json
from typing import Any

import app.state
from app.logging import Ansi
from app.logging import log

BSS_SET_ID_MIN = 1073741824

STATUS_NAMES = {
    -1: "not_submitted",
    0: "pending",
    1: "update_available",
    2: "ranked",
    3: "approved",
    4: "qualified",
    5: "loved",
}


async def notify(user_id: int, type: str, data: dict[str, Any], link: str | None = None) -> None:
    """Add a notification; never breaks the caller."""
    try:
        await app.state.services.database.execute(
            "INSERT INTO notifications (user_id, type, data, link) VALUES (:user_id, :type, :data, :link)",
            {
                "user_id": user_id,
                "type": type,
                "data": json.dumps(data, ensure_ascii=False),
                "link": link,
            },
        )
    except Exception as e:
        log(f"[notifications] could not add {type} for {user_id}: {e!r}", Ansi.LYELLOW)


async def current_first_place(map_md5: str, mode: int) -> dict[str, Any] | None:
    """The player holding #1 on a leaderboard right now (pp on rx/ap, score on vanilla)."""
    metric = "pp" if mode >= 4 else "score"
    statuses = "(2)" if metric == "pp" else "(2, 3)"
    try:
        row = await app.state.services.database.fetch_one(
            "SELECT u.id, u.name FROM scores s INNER JOIN users u ON u.id = s.userid "
            f"WHERE s.map_md5 = :map_md5 AND s.mode = :mode AND s.status IN {statuses} "
            f"AND u.priv & 1 ORDER BY s.{metric} DESC, s.id ASC LIMIT 1",
            {"map_md5": map_md5, "mode": mode},
        )
    except Exception:
        return None
    return dict(row) if row else None


async def notify_first_place_lost(previous: dict[str, Any] | None, new_holder_id: int, new_holder_name: str, bmap: Any) -> None:
    if not previous or previous["id"] == new_holder_id:
        return
    await notify(
        previous["id"],
        "first_place_lost",
        {"user": new_holder_name, "map": bmap.full_name},
        f"/b/{bmap.id}",
    )


async def notify_map_status_change(map_ids: list[int], status: int, actor_id: int | None = None) -> None:
    """Tell the owner of a BSS set that one of their maps changed status."""
    if not map_ids:
        return
    try:
        rows = await app.state.services.database.fetch_all(
            "SELECT m.id, m.set_id, m.artist, m.title, m.version, b.owner_id FROM maps m "
            "INNER JOIN bss_sets b ON b.set_id = m.set_id "
            f"WHERE m.id IN ({', '.join(str(int(i)) for i in map_ids)}) AND m.set_id >= {BSS_SET_ID_MIN}",
        )
    except Exception as e:
        log(f"[notifications] map status lookup failed: {e!r}", Ansi.LYELLOW)
        return
    status_name = STATUS_NAMES.get(int(status), str(status))
    # one notification per set (ranking a whole set changes every difficulty)
    by_set: dict[int, list[Any]] = {}
    for row in rows:
        by_set.setdefault(row["set_id"], []).append(row)
    for set_rows in by_set.values():
        first = set_rows[0]
        if actor_id is not None and first["owner_id"] == actor_id:
            continue
        name = f"{first['artist']} - {first['title']}"
        if len(set_rows) == 1:
            name += f" [{first['version']}]"
        await notify(
            first["owner_id"],
            "map_status",
            {"map": name, "status": status_name},
            f"/b/{first['id']}",
        )
