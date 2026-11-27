from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import TypedDict
from typing import cast

import app.state.services
from app._typing import UNSET
from app._typing import _UnsetSentinel
from app.repositories import Base
from app.repositories import user_achievements as ua

if TYPE_CHECKING:
    from app.objects.score import Score

from sqlalchemy import Column
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update


import ast

# TSG-32: achievement conditions come from the database and used to go straight
# into eval(), so anyone able to write that table could run any Python code in bancho.
# A condition is now checked first: only `score.<field>`, `mode_vn`, numbers,
# comparisons, and/or/not and simple arithmetic or bit operations are allowed.
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.BitAnd, ast.BitOr, ast.BitXor, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Name, ast.Attribute, ast.Constant, ast.Load,
)


def _check_cond(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"not allowed: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in ("score", "mode_vn"):
            raise ValueError(f"unknown name: {node.id}")
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id != "score" or node.attr.startswith("_"):
                raise ValueError("only score.<field> is allowed")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool, type(None))):
            raise ValueError("only numbers are allowed")


def compile_cond(cond: str) -> Callable[[Score, int], bool]:
    """Turn a condition from the database into a function, or one that is always
    False (with a log line) when the condition isn't allowed."""
    try:
        tree = ast.parse(cond, mode="eval")
        _check_cond(tree)
        code = compile(tree, "<achievement>", "eval")
    except (SyntaxError, ValueError) as e:
        from app.logging import Ansi
        from app.logging import log
        log(f"Achievement condition refused ({e}): {cond!r}", Ansi.LRED)
        return lambda score, mode_vn: False

    def cond_fn(score: Score, mode_vn: int) -> bool:
        return bool(eval(code, {"__builtins__": {}}, {"score": score, "mode_vn": mode_vn}))

    return cond_fn


class AchievementsTable(Base):
    __tablename__ = "achievements"

    id = Column("id", Integer, primary_key=True, nullable=False, autoincrement=True)
    file = Column("file", String(128), nullable=False)
    name = Column("name", String(128, collation="utf8"), nullable=False)
    desc = Column("desc", String(256, collation="utf8"), nullable=False)
    cond = Column("cond", String(64), nullable=False)

    __table_args__ = (
        Index("achievements_desc_uindex", desc, unique=True),
        Index("achievements_file_uindex", file, unique=True),
        Index("achievements_name_uindex", name, unique=True),
    )


READ_PARAMS = (
    AchievementsTable.id,
    AchievementsTable.file,
    AchievementsTable.name,
    AchievementsTable.desc,
    AchievementsTable.cond,
)


class Achievement(TypedDict):
    id: int
    file: str
    name: str
    desc: str
    cond: Callable[[Score, int], bool]


async def create(
    file: str,
    name: str,
    desc: str,
    cond: str,
) -> Achievement:
    """Create a new achievement."""
    insert_stmt = insert(AchievementsTable).values(
        file=file,
        name=name,
        desc=desc,
        cond=cond,
    )
    rec_id = await app.state.services.database.execute(insert_stmt)

    select_stmt = select(*READ_PARAMS).where(AchievementsTable.id == rec_id)
    achievement = await app.state.services.database.fetch_one(select_stmt)
    assert achievement is not None

    achievement["cond"] = compile_cond(achievement["cond"])
    return cast(Achievement, achievement)


async def fetch_one(
    id: int | None = None,
    name: str | None = None,
) -> Achievement | None:
    """Fetch a single achievement."""
    if id is None and name is None:
        raise ValueError("Must provide at least one parameter.")

    select_stmt = select(*READ_PARAMS)

    if id is not None:
        select_stmt = select_stmt.where(AchievementsTable.id == id)
    if name is not None:
        select_stmt = select_stmt.where(AchievementsTable.name == name)

    achievement = await app.state.services.database.fetch_one(select_stmt)
    if achievement is None:
        return None

    achievement["cond"] = compile_cond(achievement["cond"])
    return cast(Achievement, achievement)


async def fetch_count() -> int:
    """Fetch the number of achievements."""
    select_stmt = select(func.count().label("count")).select_from(AchievementsTable)

    rec = await app.state.services.database.fetch_one(select_stmt)
    assert rec is not None
    return cast(int, rec["count"])


async def fetch_many(
    page: int | None = None,
    page_size: int | None = None,
) -> list[Achievement]:
    """Fetch a list of achievements."""
    select_stmt = select(*READ_PARAMS)
    if page is not None and page_size is not None:
        select_stmt = select_stmt.limit(page_size).offset((page - 1) * page_size)

    achievements = await app.state.services.database.fetch_all(select_stmt)
    for achievement in achievements:
        achievement["cond"] = compile_cond(achievement["cond"])

    return cast(list[Achievement], achievements)


async def fetch_user_locked(
    user_id: int | None = None,
    *,
    UserAchievementsTable: type[ua.UserAchievementsTable],
) -> list[Achievement]:
    """Fetch a list of achievements."""

    subq = (
        select(1)
        .where(UserAchievementsTable.achid == AchievementsTable.id)
        .where(UserAchievementsTable.userid == user_id)
        .exists()
    )

    select_stmt = (select(*READ_PARAMS)).where(~subq)

    achievements = await app.state.services.database.fetch_all(select_stmt)
    for achievement in achievements:
        achievement["cond"] = compile_cond(achievement["cond"])

    return cast(list[Achievement], achievements)


async def partial_update(
    id: int,
    file: str | _UnsetSentinel = UNSET,
    name: str | _UnsetSentinel = UNSET,
    desc: str | _UnsetSentinel = UNSET,
    cond: str | _UnsetSentinel = UNSET,
) -> Achievement | None:
    """Update an existing achievement."""
    update_stmt = update(AchievementsTable).where(AchievementsTable.id == id)
    if not isinstance(file, _UnsetSentinel):
        update_stmt = update_stmt.values(file=file)
    if not isinstance(name, _UnsetSentinel):
        update_stmt = update_stmt.values(name=name)
    if not isinstance(desc, _UnsetSentinel):
        update_stmt = update_stmt.values(desc=desc)
    if not isinstance(cond, _UnsetSentinel):
        update_stmt = update_stmt.values(cond=cond)

    await app.state.services.database.execute(update_stmt)

    select_stmt = select(*READ_PARAMS).where(AchievementsTable.id == id)
    achievement = await app.state.services.database.fetch_one(select_stmt)
    if achievement is None:
        return None

    achievement["cond"] = compile_cond(achievement["cond"])
    return cast(Achievement, achievement)


async def delete_one(
    id: int,
) -> Achievement | None:
    """Delete an existing achievement."""
    select_stmt = select(*READ_PARAMS).where(AchievementsTable.id == id)
    achievement = await app.state.services.database.fetch_one(select_stmt)
    if achievement is None:
        return None

    delete_stmt = delete(AchievementsTable).where(AchievementsTable.id == id)
    await app.state.services.database.execute(delete_stmt)

    achievement["cond"] = compile_cond(achievement["cond"])
    return cast(Achievement, achievement)
