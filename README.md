# Telegram Kodi

Kodi add-on (`plugin.video.telegram`) that logs in with your own Telegram account and lists every channel, group and private chat individually, plus combined **All Images**, **All Videos** and **All Mixed** sections across all of them (newest first), and an **Unread** section: chats with unread messages and only the photos/videos posted since you last read them.

## Install (Kodi repository URL)
If the repository won't connect, choose `plugin.video.telegram-<version>.zip` from the same source in step 3 to install the add-on directly.

1. Kodi → *Settings → System → Add-ons* → turn on **Unknown sources**.
2. *Settings → File manager → Add source* → enter `https://billyke2000s.github.io/Telegram-Kodi-/` and name it `telegram`.
3. *Add-ons → Install from zip file → telegram* → `repository.telegramkodi-1.0.0.zip`.
4. *Install from repository → Telegram Kodi Repository → Video add-ons → Telegram Media → Install*.
5. Restart Kodi so the background service starts.
6. Get an API ID and API hash from https://my.telegram.org → API development tools and enter them in the add-on settings.
7. Open the add-on → **Log in with QR code** → on your phone open Telegram → *Settings → Devices → Link Desktop Device* and scan the code on the TV. If your account has a two-step verification password, Kodi asks for it next.
   (*Log in with phone number instead* is still there: phone number → code → 2FA password.)

Installing via the repository means Kodi picks up new versions automatically.

## Publishing an update
1. Bump `version` in `plugin.video.telegram/addon.xml`.
2. Run `python3 tools/build_repo.py` (regenerates `docs/`).
3. Commit and push. GitHub Pages serves `docs/` from `main`.

## Notes
- Built with Telethon 1.45.0 (bundled). Fast decryption via bundled `cryptg` for Windows x64, Python 3.11–3.14.
- Elsewhere (Android/ARM boxes, Linux) `tgkodi/fastcrypto.py` finds the OpenSSL libcrypto Kodi already has loaded and uses it for AES-IGE after a self-test; if none is found it falls back to pure Python (slow). The main menu's *crypto* line shows which is in use.
- Tapping a photo opens Kodi's slideshow over the current page, so left/right moves between photos (one page at a time).
- Videos stream through a local server on 127.0.0.1 (default port 8765) with seeking support.
- Your login is stored in the add-on profile folder as `session.txt` — keep it private.
- QR login (1.1.0) uses the bundled pure-Python `qrcode` library to draw the code. Tested against a simulated Telegram and stubbed Kodi modules only.
- Untested against live Telegram/Kodi at time of first commit.
