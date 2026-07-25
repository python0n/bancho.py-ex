# **bancho.py-ex** — taksiegra.ovh fork

![GitHub last commit](https://img.shields.io/github/last-commit/python0n/bancho.py-ex/dev?label=Last%20commit&color=1783a3)
![GitHub License](https://img.shields.io/github/license/python0n/bancho.py-ex?label=License&color=1783a3)
![Static Badge](https://img.shields.io/badge/upstream-osu--NoLimits%2Fbancho.py--ex-1783a3)
![Static Badge](https://img.shields.io/badge/base-osuAkatsuki%2Fbancho.py-1783a3)

An osu! server backend, running [osu.taksiegra.ovh](https://osu.taksiegra.ovh).

> **This is a fork of a fork.** The base project is
> [**bancho.py**](https://github.com/osuAkatsuki/bancho.py) by **cmyui** and the
> osu!Akatsuki team. [**bancho.py-ex**](https://github.com/osu-NoLimits/bancho.py-ex)
> by **osu!NoLimits** adapts it for public server hosting. This fork builds on
> bancho.py-ex with tournament client support, a full IRC multiplayer
> implementation, and a number of score-submission fixes.
>
> All of it is MIT — see [LICENSE](/LICENSE), which retains the original
> copyright.
>
> For installation, use the
> [upstream documentation](https://osu-nolimits.github.io/wiki/); this fork does
> not change the setup process.

---

## ✨ Features from bancho.py-ex

- 🔁 **Redis PubSub support** ([docs](https://github.com/osu-NoLimits/bancho.py-ex/wiki/Pubsubs)):
  - Rank or unrank beatmaps
  - Restrict / unrestrict users
  - Wipe user data
  - Send alerts to all online players
  - Grant donator status
  - Modify user privileges
- 💼 **Internal API** on `localhost:10000`
- 🪫 **PP cap autoban** (optional)
- 💬 **IRC server** (optional)
- 📊 **Prometheus metrics** (optional)
- 🥇 **Webhook for first-place scores** (optional)
- 🕹️ **osu! 2016 client support** (optional)
- ⚙️ **Server name and Discord invite configurable via `.env`** — no code changes required
- 🛠️ **Fixes and performance optimizations**

---

## **Added in this fork**

### 🏆 Tournament client support
- Authentication for the tourney client, which double-MD5s the password before sending it
- `TOURNAMENT` and `TOURNEY_MANAGER` privileges, honoured on packets 93/108/109
- Score updates forwarded to spectators; `match_start` fix
- Presence self-heal and correct handling of multiple simultaneous sessions on logout

### 💬 IRC multiplayer
- Full IRC server integration with multi-room `seq_id` support (`app/api/ircserver/`)
- `!mp` command set matching the osu! wiki, including `!mp make` and `!mp close`
- Two-way chat sync between the game client and IRC, with BanchoBot responses forwarded to IRC
- `mp_timer` and `mp_start` countdown alerts relayed to IRC clients
- Per-room slot isolation, graceful handling of an IRC user hosting a match, and match cleanup with `ended_at`

### 🎯 Scores & PP
- **PP duplication fixed** — atomic lock, reset and insert inside a transaction, ordered by PP
- `FAILED` and `SCORE_BEST` scores are now persisted instead of being silently dropped
- Leaderboard deduplication — one row per user across status 2 and 3
- **Pinned scores** — `scores.pinned` and `scores.pin_order`, populated on submission
- Self-snipe no longer triggers a "#1" announcement (`calculate_placement` ignores the player's own scores)
- `DECIMAL` PP cast to float in the `api_get_player_scores` response

### 📦 Beatmap submission
- Update-in-place for resubmitted maps, with star-rating recalculation
- Redis publish carrying `map_ids` on submission
- `osz2-service` port and MD5 mismatch on resubmission fixed

### 🔁 Redis
- `ex:map_status_change` published on rank / love / unrank
- `#announce` and `#osu` channel messages published to Redis
- Connection and pubsub leaks fixed

### 🛡️ Security & stability
- Plaintext password cache removed from `authenticate()`
- Per-IP rate limiting on login endpoints
- bcrypt cache handles `pw_bcrypt` arriving as either `str` or `bytes`
- Client hash validation normalised, so valid logins are no longer rejected
- `WEB_DOMAIN` alias and IP resolver safety fixes
- `/users/@name` redirect; "Previous #1" chat links use `osu.{domain}`

### 🎮 Multiplayer data
- Match history recorded to `mp_match_games`, `mp_match_scores`, `mp_match_events`
- Game mods saved per game, OR-ed with player mods under freemods
- `mp_match_games` written on `!mp start`, with the correct `game_id` in `_save_scores`

### Also carried from bancho.py-ex and extended
- **Beatmap Submission System** — submit from the osu! editor, `.osu` metadata parsing and star rating via `akatsuki_pp_py`, `/d/{set_id}` building `.osz` on demand, osz2-service integration
- **osu!direct mirror fallback** — failover on Cloudflare errors (520–527, 530), no-video variant via `/d/n`
- **PP sanitization** — absurd scores blocked before insertion, with configurable autoban
- **Floating-point PP** — `DECIMAL(16,3)` throughout, preventing overflow and truncation
- **Improved score webhooks** — floating-point PP, try counter in embed footers

---

## 📋 Changelog

Full changelog with all updates: **[taksiegra.ovh/changelog/](https://taksiegra.ovh/changelog/)**

---

## 🌐 Frontend

This server runs against a fork of
[Shiina-Web](https://github.com/python0n/Shiina-Web). Any other
**bancho.py**-based frontend should work as well.

---

## 📄 License

MIT — see [LICENSE](/LICENSE).

Copyright for the original work belongs to **cmyui** (2021) and the
osu!Akatsuki team. This fork is distributed under the same license, with the
original copyright notice retained as MIT requires.

---

## **Credits**

- [bancho.py](https://github.com/osuAkatsuki/bancho.py) — cmyui and osu!Akatsuki
- [bancho.py-ex](https://github.com/osu-NoLimits/bancho.py-ex) — osu!NoLimits

<a href="https://github.com/osu-NoLimits/bancho.py-ex/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=osu-NoLimits/bancho.py-ex" />
</a>

Fork maintained by [python0n](https://github.com/python0n) for
[taksiegra.ovh](https://taksiegra.ovh).