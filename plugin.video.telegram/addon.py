import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))

import xbmc  # noqa: E402
import xbmcgui  # noqa: E402
import xbmcplugin  # noqa: E402

from tgkodi import common  # noqa: E402

BASE = sys.argv[0]
HANDLE = int(sys.argv[1])
PARAMS = dict(parse_qsl(sys.argv[2].lstrip("?")))
# Kodi passes content_type=video|image when opened from Videos / Pictures add-ons.
CT = PARAMS.get("ct") or ("image" if PARAMS.get("content_type") == "image" else "video")
ADDON = common.addon()
ICON = ADDON.getAddonInfo("icon")
FANART = ADDON.getAddonInfo("fanart")


class ApiError(Exception):
    def __init__(self, status, msg):
        Exception.__init__(self, msg)
        self.status = status
        self.msg = msg


class ServiceDown(Exception):
    pass


def api(path, data=None, timeout=600):
    token = common.get_token()
    if not token:
        raise ServiceDown()
    url = "%s/api/%s" % (common.base_url(token), path)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            msg = str(e)
        raise ApiError(e.code, msg)
    except URLError:
        raise ServiceDown()


def url_for(**kw):
    kw.setdefault("ct", CT)
    return BASE + "?" + urlencode(kw)


def media_url(kind, cid, mid, ext=".jpg"):
    b = common.base_url()
    if kind == "stream":
        return "%s/stream/%d/%d/video%s" % (b, cid, mid, ext)
    if kind == "photo":
        return "%s/photo/%d/%d/image.jpg" % (b, cid, mid)
    if kind == "thumb":
        return "%s/thumb/%d/%d.jpg" % (b, cid, mid)
    if kind == "avatar":
        return "%s/avatar/%d.jpg" % (b, cid)


def notify(msg, title="Telegram", ms=5000):
    xbmcgui.Dialog().notification(title, msg, ICON, ms)


def add_dir(label, url, thumb=None, plot="", folder=True, context=None):
    li = xbmcgui.ListItem(label)
    art = {"icon": thumb or ICON, "thumb": thumb or ICON, "fanart": FANART}
    li.setArt(art)
    if plot:
        set_plot(li, plot)
    if context:
        li.addContextMenuItems(context)
    if not folder:
        li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=folder)


def set_plot(li, plot):
    try:
        li.getVideoInfoTag().setPlot(plot)
    except AttributeError:  # very old Kodi
        li.setInfo("video", {"plot": plot})


def refresh_ctx():
    return [("Refresh from Telegram", "RunPlugin(%s)" % url_for(mode="refresh"))]


def end(content=None, sort_none=True):
    if content:
        xbmcplugin.setContent(HANDLE, content)
    if sort_none:
        xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# ------------------------------------------------------------------ screens

def root():
    try:
        st = api("status", timeout=60)
    except ServiceDown:
        add_dir("[COLOR red]Background service not running[/COLOR] - restart Kodi", url_for(mode="status"),
                folder=False, plot="The add-on's service starts when Kodi starts. If you just installed it, restart Kodi.")
        return end()
    if not st.get("configured"):
        add_dir("Step 1: Enter your API ID and API hash", url_for(mode="settings"), folder=False,
                plot="Get them at https://my.telegram.org -> API development tools, then come back here.")
        return end()
    if not st.get("authorized"):
        if st.get("error"):
            add_dir("[COLOR red]%s[/COLOR]" % st["error"], url_for(mode="status"), folder=False)
        add_dir("Step 2: Log in with QR code (recommended)", url_for(mode="login_qr"), folder=False,
                plot="Shows a QR code on screen. On your phone open Telegram > Settings > Devices > "
                     "Link Desktop Device and scan it. No login code needed. If your account has a "
                     "two-step verification password you'll be asked for it afterwards.")
        add_dir("Log in with phone number instead", url_for(mode="login"), folder=False,
                plot="You'll be asked for your phone number, the code Telegram sends you, and your 2FA password if you have one.")
        add_dir("Settings", url_for(mode="settings"), folder=False)
        return end()

    try:
        dialogs = api("dialogs")["dialogs"]
    except ApiError as e:
        notify("Couldn't load chats: %s" % e.msg)
        dialogs = []

    for kind, label in (("all", "Mixed"), ("photo", "Images"), ("video", "Videos")):
        add_dir("[B]All %s[/B]" % label, url_for(mode="media", chats="all", kind=kind, title="All " + label),
                plot="%s from every included chat, newest first." % label, context=refresh_ctx())

    inc = {"channel": common.setting_bool("include_channels"),
           "group": common.setting_bool("include_groups"),
           "user": common.setting_bool("include_private")}
    groups = [d for d in dialogs if d["type"] == "group"]
    users = [d for d in dialogs if d["type"] == "user"]
    if inc["group"] and groups:
        add_dir("[B]Groups (%d)[/B]" % len(groups), url_for(mode="chats", type="group"), context=refresh_ctx())
    if inc["user"] and users:
        add_dir("[B]Private chats (%d)[/B]" % len(users), url_for(mode="chats", type="user"), context=refresh_ctx())
    if inc["channel"]:
        for d in dialogs:
            if d["type"] == "channel":
                add_chat(d)

    add_dir("[I]Refresh[/I]", url_for(mode="refresh"), folder=False)
    add_dir("[I]Account: %s  |  crypto: %s[/I]" % (st.get("user", "?"), st.get("crypto", "?")),
            url_for(mode="status"), folder=False)
    add_dir("[I]Settings[/I]", url_for(mode="settings"), folder=False)
    add_dir("[I]Log out[/I]", url_for(mode="logout"), folder=False)
    end()


def add_chat(d):
    thumb = media_url("avatar", d["id"], 0) if d.get("has_photo") else None
    label = d["title"]
    if d.get("unread"):
        label += "  [COLOR grey](%d unread)[/COLOR]" % d["unread"]
    add_dir(label, url_for(mode="chat", id=d["id"], title=d["title"]), thumb=thumb, context=refresh_ctx())


def chats(typ):
    dialogs = api("dialogs")["dialogs"]
    for d in dialogs:
        if d["type"] == typ:
            add_chat(d)
    end()


def chat(cid, title):
    for kind, label in (("all", "Mixed"), ("photo", "Images"), ("video", "Videos")):
        add_dir(label, url_for(mode="media", chats=str(cid), kind=kind, title="%s - %s" % (title, label)),
                context=refresh_ctx())
    end()


def fmt_dur(sec):
    if not sec:
        return ""
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def media(chats_param, kind, cursor, title):
    q = {"chats": chats_param, "kind": kind, "n": common.setting_int("page_size", 50, 10, 200)}
    if cursor:
        q["cursor"] = cursor
    res = api("media?" + urlencode(q))
    combined = chats_param == "all"
    for it in res["items"]:
        add_media_item(it, combined)
    if res.get("next"):
        add_dir("[B]Next page >>[/B]", url_for(mode="media", chats=chats_param, kind=kind,
                                              cursor=res["next"], title=title))
    end("images" if CT == "image" else "videos")


def add_media_item(it, combined):
    cid, mid = it["chat_id"], it["msg_id"]
    text = (it.get("text") or "").strip()
    first = text.splitlines()[0][:90] if text else ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it["date"]))
    label = first or it.get("name") or ("Photo" if it["kind"] == "photo" else "Video") + " " + when
    if it["kind"] == "video" and it.get("duration"):
        label += "  [COLOR grey](%s)[/COLOR]" % fmt_dur(it["duration"])
    if combined and it.get("chat_title"):
        label = "[COLOR grey]%s:[/COLOR] %s" % (it["chat_title"], label)
    thumb = media_url("thumb", cid, mid)
    li = xbmcgui.ListItem(label, when)
    try:
        li.setDateTime(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(it["date"])))
    except Exception:
        pass

    if it["kind"] == "photo":
        full = media_url("photo", cid, mid)
        li.setArt({"thumb": thumb, "icon": thumb, "poster": thumb, "fanart": full})
        if CT == "image":
            li.setInfo("pictures", {"title": label})
            li.setMimeType("image/jpeg")
            path = full
        else:
            set_plot(li, text)
            li.setProperty("IsPlayable", "false")
            path = url_for(mode="show_image", url=full)
        xbmcplugin.addDirectoryItem(HANDLE, path, li, isFolder=False)
        return

    path = media_url("stream", cid, mid, it.get("ext") or ".mp4")
    li.setArt({"thumb": thumb, "icon": thumb, "poster": thumb})
    li.setMimeType(it.get("mime") or "video/mp4")
    li.setContentLookup(False)
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(first or it.get("name") or "Video " + when)
        tag.setPlot(text)
        if it.get("duration"):
            tag.setDuration(int(it["duration"]))
        tag.setMediaType("video")
    except AttributeError:
        li.setInfo("video", {"title": first or it.get("name") or "Video " + when, "plot": text, "duration": it.get("duration") or 0})
    xbmcplugin.addDirectoryItem(HANDLE, path, li, isFolder=False)


# ------------------------------------------------------------------ actions

class QrWindow(xbmcgui.WindowDialog):
    """Full-screen QR code. WindowDialog uses a 1280x720 coordinate space."""
    CENTER = 0x00000002  # XBFONT_CENTER_X
    BACK_ACTIONS = (9, 10, 92, 13)  # parent dir, previous menu, back, stop

    def __init__(self, backdrop):
        xbmcgui.WindowDialog.__init__(self)
        self.closed = False
        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, backdrop, colorDiffuse="F0000000"))
        self.addControl(xbmcgui.ControlLabel(
            0, 40, 1280, 40, "[B]Log in to Telegram[/B]", font="font30",
            textColor="0xFFFFFFFF", alignment=self.CENTER))
        self.qr = xbmcgui.ControlImage(450, 110, 380, 380, "")
        self.addControl(self.qr)
        self.status = xbmcgui.ControlLabel(
            0, 505, 1280, 30, "Getting a code from Telegram...", font="font13",
            textColor="0xFFFFFFFF", alignment=self.CENTER)
        self.addControl(self.status)
        for i, line in enumerate((
                "On your phone open Telegram > Settings > Devices > Link Desktop Device",
                "and point the camera at this code. It refreshes automatically.",
                "Press Back to cancel.")):
            self.addControl(xbmcgui.ControlLabel(
                0, 550 + i * 32, 1280, 30, line, font="font13",
                textColor="0xFFCCCCCC", alignment=self.CENTER))

    def set_qr(self, path):
        self.qr.setImage(path, False)
        self.status.setLabel("Waiting for you to scan...")

    def onAction(self, action):
        if action.getId() in self.BACK_ACTIONS:
            self.closed = True
            self.close()


def _qr_files():
    from tgkodi import qrimage
    folder = os.path.join(common.profile_dir(), "qr")
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):  # old codes are useless once replaced
        if name.startswith("qr_"):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    backdrop = os.path.join(folder, "backdrop.png")
    if not os.path.exists(backdrop):
        qrimage.write_solid(backdrop)
    return folder, backdrop


def login_qr():
    from tgkodi import qrimage
    dlg = xbmcgui.Dialog()
    try:
        st = api("login/qr/start", {}, timeout=60)
    except ApiError as e:
        dlg.ok("Telegram", "Couldn't start QR login:\n%s" % e.msg)
        return
    if st.get("state") == "done":
        notify("Already logged in")
        xbmc.executebuiltin("Container.Refresh")
        return

    folder, backdrop = _qr_files()
    win = QrWindow(backdrop)
    win.show()
    monitor = xbmc.Monitor()
    shown, n = None, 0
    try:
        while not win.closed and not monitor.abortRequested():
            state = st.get("state")
            if state == "waiting" and st.get("url") != shown:
                n += 1
                # Unique name per code, otherwise Kodi's texture cache shows the old one.
                path = os.path.join(folder, "qr_%d_%d.png" % (int(time.time()), n))
                qrimage.write_qr(st["url"], path)
                win.set_qr(path)
                shown = st["url"]
            elif state not in ("waiting", "starting"):
                break
            if monitor.waitForAbort(1):
                break
            st = api("login/qr/status", timeout=30)
    finally:
        cancelled = win.closed
        win.close()
        del win
        _qr_files()

    state = st.get("state")
    if cancelled or state in ("waiting", "starting"):
        try:
            api("login/qr/cancel", {}, timeout=30)
        except (ApiError, ServiceDown):
            pass
        return
    if state == "need_password":
        if not _ask_password(dlg):
            return
    elif state == "done":
        notify("Logged in")
    elif state == "expired":
        dlg.ok("Telegram", "The QR code timed out. Choose 'Log in with QR code' to try again.")
        return
    else:
        dlg.ok("Telegram", "QR login failed:\n%s" % (st.get("error") or state))
        return
    xbmc.executebuiltin("Container.Refresh")


def _ask_password(dlg):
    """Two-step verification. Returns True once logged in."""
    pw = dlg.input("Two-step verification password (your Telegram cloud password)",
                   option=xbmcgui.ALPHANUM_HIDE_INPUT)
    if not pw:
        return False
    try:
        res = api("login/password", {"password": pw}, timeout=120)
    except ApiError as e:
        dlg.ok("Telegram", "Login failed:\n%s" % e.msg)
        return False
    if res.get("ok"):
        notify("Logged in")
        return True
    dlg.ok("Telegram", "Login did not complete.")
    return False


def login():
    dlg = xbmcgui.Dialog()
    phone = dlg.input("Phone number in international format (e.g. +447700900123)")
    if not phone:
        return
    try:
        api("login/send_code", {"phone": phone}, timeout=120)
    except ApiError as e:
        dlg.ok("Telegram", "Couldn't send code:\n%s" % e.msg)
        return
    code = dlg.input("Login code (sent to your Telegram app / SMS)", type=xbmcgui.INPUT_NUMERIC)
    if not code:
        return
    try:
        res = api("login/code", {"code": code}, timeout=120)
        if res.get("need_password"):
            pw = dlg.input("Two-step verification password", option=xbmcgui.ALPHANUM_HIDE_INPUT)
            if not pw:
                return
            res = api("login/password", {"password": pw}, timeout=120)
    except ApiError as e:
        dlg.ok("Telegram", "Login failed:\n%s" % e.msg)
        return
    if res.get("ok"):
        notify("Logged in")
    else:
        dlg.ok("Telegram", "Login did not complete.")
    xbmc.executebuiltin("Container.Refresh")


def logout():
    if not xbmcgui.Dialog().yesno("Telegram", "Log out and delete the saved session on this device?"):
        return
    try:
        api("logout", {}, timeout=60)
    except (ApiError, ServiceDown) as e:
        notify("Logout error: %s" % e)
    xbmc.executebuiltin("Container.Refresh")


def status():
    try:
        st = api("status", timeout=60)
        msg = "\n".join("%s: %s" % (k, v) for k, v in st.items())
    except ServiceDown:
        msg = "Background service is not reachable on 127.0.0.1:%d.\nRestart Kodi." % common.port()
    xbmcgui.Dialog().textviewer("Telegram status", msg)


def router():
    mode = PARAMS.get("mode")
    try:
        if mode is None:
            root()
        elif mode == "chats":
            chats(PARAMS.get("type", "group"))
        elif mode == "chat":
            chat(int(PARAMS["id"]), PARAMS.get("title", ""))
        elif mode == "media":
            media(PARAMS.get("chats", "all"), PARAMS.get("kind", "all"), PARAMS.get("cursor"), PARAMS.get("title", ""))
        elif mode == "show_image":
            xbmc.executebuiltin("ShowPicture(%s)" % PARAMS["url"])
        elif mode == "login":
            login()
        elif mode == "login_qr":
            login_qr()
        elif mode == "logout":
            logout()
        elif mode == "settings":
            ADDON.openSettings()
            xbmc.executebuiltin("Container.Refresh")
        elif mode == "status":
            status()
        elif mode == "refresh":
            api("refresh", {}, timeout=60)
            xbmc.executebuiltin("Container.Refresh")
    except ServiceDown:
        notify("Background service not running - restart Kodi")
        if mode in ("chats", "chat", "media"):
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
    except ApiError as e:
        if e.status == 401:
            notify("Not logged in")
        else:
            notify("Error: %s" % e.msg)
        if mode in ("chats", "chat", "media"):
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


router()
