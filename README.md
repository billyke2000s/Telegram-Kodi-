# Telegram Kodi

Kodi add-on (`plugin.video.telegram`) that logs in with your own Telegram account and lists every channel, group and private chat individually, plus combined **All Images**, **All Videos** and **All Mixed** sections across all of them.

## Install (Kodi repository URL)
1. Kodi → *Settings → System → Add-ons* → turn on **Unknown sources**.
2. *Settings → File manager → Add source* → enter `https://billyke2000s.github.io/Telegram-Kodi-/` and name it `telegram`.
3. *Add-ons → Install from zip file → telegram* → `repository.telegramkodi-1.0.0.zip`.
4. *Install from repository → Telegram Kodi Repository → Video add-ons → Telegram Media → Install*.
5. Restart Kodi so the background service starts.
6. Get an API ID and API hash from https://my.telegram.org → API development tools and enter them in the add-on settings.
7. Open the add-on → *Log in* → phone number → code → 2FA password (if set).

Installing via the repository means Kodi picks up new versions automatically.

## Publishing an update
1. Bump `version` in `plugin.video.telegram/addon.xml`.
2. Run `python3 tools/build_repo.py` (regenerates `docs/`).
3. Commit and push. GitHub Pages serves `docs/` from `main`.

## Notes
- Built with Telethon 1.45.0 (bundled). Fast decryption via bundled `cryptg` for Windows x64, Python 3.11–3.14.
- Videos stream through a local server on 127.0.0.1 (default port 8765) with seeking support.
- Your login is stored in the add-on profile folder as `session.txt` — keep it private.
- Untested against live Telegram/Kodi at time of first commit.
