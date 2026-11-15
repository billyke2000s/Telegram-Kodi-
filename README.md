# Telegram Kodi

Kodi add-on (`plugin.video.telegram`) that logs in with your own Telegram account and lists every channel, group and private chat individually, plus combined **All Images**, **All Videos** and **All Mixed** sections across all of them.

## Install
1. Zip the `plugin.video.telegram` folder (or grab a release zip) and in Kodi use *Add-ons → Install from zip file*.
2. Restart Kodi so the background service starts.
3. Get an API ID and API hash from https://my.telegram.org → API development tools and enter them in the add-on settings.
4. Open the add-on → *Log in* → phone number → code → 2FA password (if set).

## Notes
- Built with Telethon 1.45.0 (bundled). Fast decryption via bundled `cryptg` for Windows x64, Python 3.11–3.14.
- Videos stream through a local server on 127.0.0.1 (default port 8765) with seeking support.
- Your login is stored in the add-on profile folder as `session.txt` — keep it private.
- Untested against live Telegram/Kodi at time of first commit.
