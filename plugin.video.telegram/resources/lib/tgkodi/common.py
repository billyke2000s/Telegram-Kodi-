"""Shared helpers for the plugin (addon.py) and the background service (service.py)."""
import os
import sys
import secrets
import struct

import xbmc
import xbmcaddon
import xbmcvfs

ADDON_ID = "plugin.video.telegram"
LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # resources/lib


def addon():
    # A fresh Addon() object always reflects the latest saved settings.
    return xbmcaddon.Addon(ADDON_ID)


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), level)


def profile_dir():
    path = xbmcvfs.translatePath(addon().getAddonInfo("profile"))
    os.makedirs(path, exist_ok=True)
    return path


def setting(key, default=""):
    try:
        val = addon().getSetting(key)
    except Exception:
        return default
    return val if val != "" else default


def setting_int(key, default, lo=None, hi=None):
    try:
        val = int(setting(key, str(default)))
    except (TypeError, ValueError):
        val = default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def setting_bool(key, default=True):
    val = setting(key, "")
    if val == "":
        return default
    return val.lower() == "true"


def port():
    return setting_int("port", 8765, 1024, 65535)


TOKEN_FILE = "server_token.txt"


def get_token(create=False):
    """Random token that every local HTTP request must carry, so other programs
    on the machine can't pull your Telegram media from the local server."""
    path = os.path.join(profile_dir(), TOKEN_FILE)
    if os.path.exists(path):
        with open(path, "r") as f:
            tok = f.read().strip()
        if tok:
            return tok
    if not create:
        return ""
    tok = secrets.token_hex(16)
    with open(path, "w") as f:
        f.write(tok)
    return tok


def base_url(token=None):
    return "http://127.0.0.1:%d/t%s" % (port(), token if token is not None else get_token())


def setup_native_crypto():
    """Put the matching prebuilt `cryptg` (fast AES-IGE) on sys.path if one is bundled
    for this platform/Python. Without it Telethon falls back to pure-Python AES,
    which measured ~0.5 MB/s in testing - too slow for HD video."""
    plat = None
    if sys.platform.startswith("win") and struct.calcsize("P") == 8:
        plat = "win_amd64"
    if not plat:
        return False
    tag = "cp%d%d" % sys.version_info[:2]
    path = os.path.join(LIB_DIR, "_native", plat, tag)
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)
        return True
    return False
