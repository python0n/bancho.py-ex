# bancho.py-ex - taksiegra fork

![GitHub last commit](https://img.shields.io/github/last-commit/python0n/bancho.py-ex/dev?label=Last%20commit&color=1783a3)
![GitHub License](https://img.shields.io/github/license/python0n/bancho.py-ex?label=License&color=1783a3)
![Upstream](https://img.shields.io/badge/upstream-osu--NoLimits%2Fbancho.py--ex-1783a3)
![Base](https://img.shields.io/badge/base-osuAkatsuki%2Fbancho.py-1783a3)

osu! server behind [taksiegra](https://osu.taksiegra.ovh). It is a fork of
[bancho.py-ex](https://github.com/osu-NoLimits/bancho.py-ex) by osu!NoLimits, which itself is built on
[bancho.py](https://github.com/osuAkatsuki/bancho.py) by cmyui and the osu!Akatsuki team.

Installation works like upstream, so use the [upstream wiki](https://osu-nolimits.github.io/wiki/).
This page lists what the fork changes. The version in `pyproject.toml` (5.x) is the one shown in our
changelog.

## From bancho.py-ex

- Redis pubsub for ranking maps, restricting, wiping, alerts, donator status and privileges
  ([docs](https://github.com/osu-NoLimits/bancho.py-ex/wiki/Pubsubs)).
- Internal API on `localhost:10000`.
- Optional: pp cap autoban, IRC server, Prometheus metrics, webhook for first places, osu! 2016 client.
- Server name and Discord invite set in `.env`.
- Beatmap submission from the osu! editor (BSS), `.osz` built on demand at `/d/<set_id>`.
- pp stored as `DECIMAL(16,3)` and absurd scores blocked before they are saved.

## What the fork adds

### Beatmap submission (BSS)
- Every uploaded set has an owner (`bss_sets` table). Only the owner can update a set, only inside
  the BSS id range, and restricted players cannot upload at all.
- File type allow list and size limits for the `.osz` contents; storyboard subfolders are kept.
- A ranked set that gets updated goes back to Pending.
- Star rating and max combo of each difficulty from osz2-service, which is reached over the
  Docker network only (no public port).
- After an update the leaderboard of the right difficulty is used, not always the first one.

### Scores and pp
- No duplicated pp: the best score is chosen and saved inside one transaction.
- Failed scores and best-by-score scores are saved instead of dropped.
- One leaderboard row per player.
- In score based modes the #1, placements, personal bests and #1 announcements go by the highest
  score; Relax and Autopilot go by pp. Beating your own #1 is not announced.
- Pinned scores (`scores.pinned`, `scores.pin_order`).
- Replays uploaded on the website and accepted by an admin are saved through `POST /v1/approve_replay`
  (website only, `X-Internal-Key`) with the same rules as scores from the game, stats and rank included.

### Multiplayer
- A match never hangs: leaving or losing connection while loading, skipping or playing finishes
  that step for everyone else.
- Match results are saved from the score frames the client sends, like official bancho, with
  accuracy and grade calculated the osu-web way, mods (ScoreV2 included) and the pass state.
- Results in the match chat are sorted by the win condition; the history link points to the
  match page on the website.
- `!mp` commands follow the osu! wiki. Rooms made with `!mp make` close after 30 minutes
  without players. Locking a taken slot and `!mp kick` really remove the player.
- Match history in `mp_match_games`, `mp_match_scores` and `mp_match_events`.
- Tournament client: login, `TOURNAMENT` and `TOURNEY_MANAGER` privileges, scores for spectators.

### IRC
- The same rules as in the game: silence, read only channels, blocked private messages,
  2000 character limit; messages to offline players go to their mailbox.
- Private messages between IRC users and from the game to IRC users work, so do joining and
  leaving channels.

### Website chat
- `/ws/chat`: the IRC protocol over a WebSocket, used by the chat page of our Shiina. The connection is
  a normal client of the IRC server, so channels, PMs, mail, silences, blocks and `!mp` work the same.
  The website logs the player in with a one-time ticket in Redis (`chat:ticket:<ticket>`), never with
  the IRC key. Needs the `websockets` package, installed by the Dockerfile.
- Private messages from the game, IRC and the website are kept in `mail` (PM history); the last 100
  messages of public channels are kept in Redis for 7 days (`chat:history:<channel>`).
- Logging into the game while only IRC or the website chat is open takes that session over instead of
  failing with "User already logged in"; closing the game keeps the chat connected.
- Messages to offline players really go to their mailbox (offline players were seen as online before).

### Accounts and moderation
- Restricted players are hidden from other players in the public `/v1` API. The website sends
  `X-Internal-Key` (`INTERNAL_API_KEY` in `.env`) to see them for staff.
- A wrong password no longer logs out an idle session of the same account.
- Login rate limit per IP; no plaintext password cache.
- Clan changes made on the website reach the game at once (`clan_change` pubsub).

### Redis
- `ex:map_status_change` on rank, love and unrank; `#announce` and `#osu` messages; BSS uploads.
  Our Discord bot listens to these.
- Connection and pubsub leaks fixed.

## Settings added in `.env`

| Setting | What it does |
| --- | --- |
| `INTERNAL_API_KEY` | key the website sends to see restricted players and to save accepted replays; same value in Shiina's `.env` |
| `OSZ2_SERVICE_URL` | address of osz2-service, e.g. `http://osz2-service:8080` |

## Frontend

The website is our fork of [Shiina-Web](https://github.com/python0n/Shiina-Web). Other bancho.py
frontends should work too.

## Changelog

[taksiegra.ovh/changelog](https://taksiegra.ovh/changelog/)

## License and credits

MIT, see [LICENSE](/LICENSE). The original work is copyright cmyui (2021) and the osu!Akatsuki team;
this fork keeps the notice and the same licence.

- [bancho.py](https://github.com/osuAkatsuki/bancho.py) - cmyui and osu!Akatsuki
- [bancho.py-ex](https://github.com/osu-NoLimits/bancho.py-ex) - osu!NoLimits

<a href="https://github.com/osu-NoLimits/bancho.py-ex/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=osu-NoLimits/bancho.py-ex" alt="bancho.py-ex contributors" />
</a>

Fork maintained by [python0n](https://github.com/python0n).
