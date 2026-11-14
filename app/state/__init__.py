from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

from . import cache
from . import services
from . import sessions

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from app.packets import BasePacket
    from app.packets import ClientPackets

loop: AbstractEventLoop
# {score checksum: [lock, number of submissions holding or waiting for it]}
_score_submission_locks: dict[str, list[Any]] = {}


@contextlib.asynccontextmanager
async def score_submission_lock(checksum: str) -> AsyncIterator[None]:
    """Lock around one score checksum, so the same score is not saved twice.

    The checksum comes from the client, so the entry is removed again when
    nobody uses it; otherwise every submitted score would stay in memory.
    """
    entry = _score_submission_locks.get(checksum)
    if entry is None:
        entry = _score_submission_locks[checksum] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _score_submission_locks.pop(checksum, None)


packets: dict[Literal["all", "restricted"], dict[ClientPackets, type[BasePacket]]] = {
    "all": {},
    "restricted": {},
}
shutting_down = False
