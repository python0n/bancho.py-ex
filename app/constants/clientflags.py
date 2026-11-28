from __future__ import annotations

from enum import IntFlag
from enum import unique

from app.utils import escape_enum
from app.utils import pymysql_encode


@unique
@pymysql_encode(escape_enum)
class ClientFlags(IntFlag):
    """osu! anticheat <= 2016 (unsure of age)"""

    # NOTE: many of these flags are quite outdated and/or
    # broken and are even known to false positive quite often.
    # they can be helpful; just take them with a grain of salt.

    CLEAN = 0  # no flags sent

    # flags for timing errors or desync.
    SPEED_HACK_DETECTED = 1 << 1

    # this is to be ignored by server implementations. osu! team trolling hard
    INCORRECT_MOD_VALUE = 1 << 2

    MULTIPLE_OSU_CLIENTS = 1 << 3
    CHECKSUM_FAILURE = 1 << 4
    FLASHLIGHT_CHECKSUM_INCORRECT = 1 << 5

    # these are only used on the osu!bancho official server.
    OSU_EXECUTABLE_CHECKSUM = 1 << 6
    MISSING_PROCESSES_IN_LIST = 1 << 7  # also deprecated as of 2018

    # flags for either:
    # 1. pixels that should be outside the visible radius
    # (and thus black) being brighter than they should be.
    # 2. from an internal alpha value being incorrect.
    FLASHLIGHT_IMAGE_HACK = 1 << 8

    SPINNER_HACK = 1 << 9
    TRANSPARENT_WINDOW = 1 << 10

    # (mania) flags for consistently low press intervals.
    FAST_PRESS = 1 << 11

    # from my experience, pretty decent
    # for detecting autobotted scores.
    RAW_MOUSE_DISCREPANCY = 1 << 12
    RAW_KEYBOARD_DISCREPANCY = 1 << 13


@unique
@pymysql_encode(escape_enum)
class LastFMFlags(IntFlag):
    """Flags sent to /web/lastfm.php as "a" + number.

    The number can carry the old (2016) bits as well as the 2019 ones, so both
    are named here (Akatsuki's score-service reads them the same way).
    """

    # 2016 anticheat, same bits as ClientFlags
    SPEED_HACK_DETECTED = 1 << 1
    INCORRECT_MOD_VALUE = 1 << 2
    MULTIPLE_OSU_CLIENTS = 1 << 3
    CHECKSUM_FAILURE = 1 << 4
    FLASHLIGHT_CHECKSUM_INCORRECT = 1 << 5
    OSU_EXECUTABLE_CHECKSUM = 1 << 6
    MISSING_PROCESSES_IN_LIST = 1 << 7
    FLASHLIGHT_IMAGE_HACK = 1 << 8
    SPINNER_HACK = 1 << 9
    TRANSPARENT_WINDOW = 1 << 10
    FAST_PRESS = 1 << 11
    RAW_MOUSE_DISCREPANCY = 1 << 12
    RAW_KEYBOARD_DISCREPANCY = 1 << 13

    # 2019 anticheat
    # XXX: the aqn flags were fixed within hours of the osu!
    # update, and vanilla hq is not so widely used anymore.
    RUN_WITH_LD_FLAG = 1 << 14
    CONSOLE_OPEN = 1 << 15
    EXTRA_THREADS = 1 << 16
    HQ_ASSEMBLY = 1 << 17
    HQ_FILE = 1 << 18
    REGISTRY_EDITS = 1 << 19
    SDL2_LIBRARY = 1 << 20
    OPENSSL_LIBRARY = 1 << 21
    AQN_MENU_SAMPLE = 1 << 22


# Short notes for the staff alerts: what a flag means and how much to trust it.
FLAG_NOTES: dict[int, str] = {
    1 << 1: "game speed did not match the music (speed hack, but lag can cause it too)",
    1 << 2: "mods value did not match the enabled mods (mod remover?)",
    1 << 3: "more than one osu! client running",
    1 << 4: "score values in memory changed in an odd way (memory editing)",
    1 << 5: "flashlight checksum wrong (unclear meaning)",
    1 << 6: "osu! executable checksum (official server only)",
    1 << 7: "missing processes in the list (official server only, old)",
    1 << 8: "flashlight area brighter than it should be (flashlight remover, reliable)",
    1 << 9: "spinner hack",
    1 << 10: "a transparent window over the game (cheat menu?)",
    1 << 11: "mania: presses consistently too fast",
    1 << 12: "cursor moved differently than the raw mouse input (bot/relax tools; tablet drivers and some maps can cause it)",
    1 << 13: "keys pressed differently than the raw keyboard input (key automation?)",
    1 << 14: "client run with an LD flag (unclear meaning)",
    1 << 15: "a console attached to osu! (debugging)",
    1 << 16: "extra threads in the osu! process (injected cheat?)",
    1 << 17: "hq!osu assembly loaded",
    1 << 18: "hq!osu files found",
    1 << 19: "registry traces of hq!osu relife (multi-account tool, maybe old)",
    1 << 20: "old AQN library (SDL2)",
    1 << 21: "old AQN library (libeay32)",
    1 << 22: "old AQN menu sound",
}


def describe_flags(value: int, names: type[IntFlag] = LastFMFlags) -> str:
    """'NAME: note; ...' for every set bit, unknown bits by number."""
    parts: list[str] = []
    for bit in range(64):
        b = 1 << bit
        if not value & b:
            continue
        try:
            name = names(b).name
        except ValueError:
            name = None
        if not name:
            parts.append(f"unknown bit {bit}")
            continue
        note = FLAG_NOTES.get(b)
        parts.append(f"{name}: {note}" if note else name)
    return "; ".join(parts) if parts else f"flags {value}"
