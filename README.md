# bancho.py-ex
![Discord](https://img.shields.io/discord/1295422749807743037?label=Discord&link=https%3A%2F%2Fdiscord.gg%2F6DH8bB24p6&color=1783a3&link=https%3A%2F%2Fgithub.com%2Fosu-NoLimits%2FShiina-Web)
![GitHub contributors](https://img.shields.io/github/contributors/osu-NoLimits/bancho.py-ex?label=Contributors&color=1783a3&link=https%3A%2F%2Fgithub.com%2Fosu-NoLimits%2Fbancho.py-ex)
![GitHub License](https://img.shields.io/github/license/osu-NoLimits/Shiina-Web?label=License&color=1783a3&link=https%3A%2F%2Fgithub.com%2Fosu-NoLimits%2Fbancho.py-ex)
![GitHub Created At](https://img.shields.io/github/created-at/osu-NoLimits/bancho.py-ex?label=Created&color=1783a3&link=https%3A%2F%2Fgithub.com%2Fosu-NoLimits%2Shiina-Web)
![Static Badge](https://img.shields.io/badge/available%20-%20Test?label=Documentation&color=1783a3&link=https%3A%2F%2Fosu-nolimits.github.io%2Fwiki%2F)
![GitHub Repo stars](https://img.shields.io/github/stars/osu-NoLimits/bancho.py-ex)

**bancho.py-ex** is a fork of [**bancho.py**](https://github.com/osuAkatsuki), designed to be more suitable for public osu! server hosting.

---

## 🛠️ Installation

For installation look up our [new documentation](https://osu-nolimits.github.io/wiki/)

---

## ✨ Features

- 🔁 **Redis PubSub support** ([Docs](https://github.com/osu-NoLimits/bancho.py-ex/wiki/Pubsubs)):
  - Rank or unrank beatmaps
  - Restrict / unrestrict users
  - Wipe user data
  - Send alerts to all online players
  - Grant donator status
  - Modify user privileges
- 💼 **Access to API on localhost:10000**
- 🪫 **Set PP Cap Autoban** (optional)
- 💬 **IRC Server** (optional)
- 📊 **Prometheus metrics** (optional)
- 🥇 **Webhook for first-place scores** (optional)
- 🕹️ **Support for osu! 2016 client** (optional)
- ⚙️ **Configure server name and Discord invite via `.env`—no code changes required**
- 🛠️ **Fixes and performance optimizations**
- 📦 **Beatmap Submission System (BSS)**:
  - Submit maps directly from the osu! editor
  - Automatic `.osu` metadata parsing and star rating calculation via `akatsuki_pp_py`
  - Map download endpoint `/d/{set_id}` that builds `.osz` files on demand
  - Integration with osz2-service for encrypted file decryption
- 🪞 **osu!direct mirror fallback** — automatic failover on Cloudflare errors (520–527, 530), no-video variant via `/d/n`
- 🛡️ **Per-IP rate limiting** — brute-force protection on login endpoints
- 🧹 **PP sanitization** — blocks absurd PP scores from entering the database with configurable autoban
- 🔢 **Floating-point PP** — stored as `DECIMAL(16,3)` throughout to prevent overflow and truncation
- 🎮 **Match history tracking** — records multiplayer match data (`mp_match_games`, `mp_match_scores`, `mp_match_events`)
- 🔐 **Client hash validation** — normalized hash comparison to prevent false login rejections
- 📬 **Improved score webhooks** — floating-point PP display, try counter in embed footers

---

## 📋 Changelog

Full changelog with all updates: **[taksiegra.ovh/changelog/](https://taksiegra.ovh/changelog/)**

---

## 🌐 Frontend Support

- 🖼️ [Shiina Web (Official)](https://github.com/osu-NoLimits/Shiina-Web/tree/main)
- ✔️ Compatible with all other **bancho.py**-based frontends

---

## 📄 License

Licensed under the [MIT License](https://opensource.org/license/mit/).  
See the [LICENSE](https://github.com/osuAkatsuki/bancho.py/blob/master/LICENSE) for more details.

---

## **Contributors**


<a href="https://github.com/osu-NoLimits/bancho.py-ex/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=osu-NoLimits/bancho.py-ex" />
</a>