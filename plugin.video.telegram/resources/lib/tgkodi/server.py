"""Background engine run by service.py.

* One Telethon client (your user account) on a private asyncio loop thread.
* A tiny HTTP server on 127.0.0.1 that:
    - answers JSON API calls from the plugin (dialogs, media pages, login)
    - streams videos with HTTP Range support (so Kodi can seek)
    - serves photos, thumbnails and chat avatars (cached on disk)

Only one process ever touches the Telegram session, which avoids session-file
locking problems between Kodi's plugin invocations.
"""
import asyncio
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qs, unquote

from telethon import TelegramClient, errors, types, utils
from telethon.sessions import StringSession

from .mediaindex import MediaIndex, decode_cursor

CHUNK = 512 * 1024  # Telegram's max upload.getFile request size
SESSION_FILE = "session.txt"

FILTERS = {
    "photo": types.InputMessagesFilterPhotos,
    "video": types.InputMessagesFilterVideo,
    "all": types.InputMessagesFilterPhotoVideo,
}


# ---------------------------------------------------------------- pure helpers

def parse_range(header, size):
    """Return (start, end, partial) for an HTTP Range header. Raises ValueError -> 416."""
    if not header:
        return 0, size - 1, False
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return 0, size - 1, False  # ignore multi-range / unknown units: send whole file
    a, _, b = spec.strip().partition("-")
    if a == "":
        if b == "":
            raise ValueError("empty range")
        n = int(b)
        if n <= 0:
            raise ValueError("bad suffix")
        start, end = max(0, size - n), size - 1
    else:
        start = int(a)
        end = int(b) if b else size - 1
        end = min(end, size - 1)
    if start >= size or start > end:
        raise ValueError("unsatisfiable")
    return start, end, True


def pick_thumb_type(sizes, max_w=640):
    """Choose a reasonable JPEG thumbnail size type ('m', 'x', ...)."""
    cands = []
    for s in sizes or []:
        if isinstance(s, (types.PhotoSize, types.PhotoSizeProgressive)) and getattr(s, "w", 0):
            cands.append(s)
    if not cands:
        return None
    small_enough = [s for s in cands if s.w <= max_w]
    best = max(small_enough, key=lambda s: s.w) if small_enough else min(cands, key=lambda s: s.w)
    return best.type


def message_to_item(m, chat_id, chat_title=""):
    """Convert a Telethon message to a plain dict, or None if it isn't a photo/video."""
    kind = None
    if isinstance(m.media, types.MessageMediaPhoto) and isinstance(m.media.photo, types.Photo):
        kind = "photo"
    elif isinstance(m.media, types.MessageMediaDocument) and m.video and not m.gif and not m.video_note:
        kind = "video"
    if not kind:
        return None
    f = m.file
    item = {
        "chat_id": chat_id,
        "chat_title": chat_title,
        "msg_id": m.id,
        "date": int(m.date.timestamp()) if m.date else 0,
        "kind": kind,
        "text": m.message or "",
        "size": (f.size if f else 0) or 0,
        "w": (f.width if f else 0) or 0,
        "h": (f.height if f else 0) or 0,
        "duration": int((f.duration if f else 0) or 0),
        "mime": (f.mime_type if f else "") or ("image/jpeg" if kind == "photo" else "video/mp4"),
        "name": (f.name if f else "") or "",
        "ext": ".jpg",
    }
    if kind == "video":
        ext = os.path.splitext(item["name"])[1].lower()
        if not (2 <= len(ext) <= 6 and ext[1:].isalnum()):
            ext = VIDEO_EXT.get(item["mime"].lower(), ".mp4")
        item["ext"] = ext
    return item


VIDEO_EXT = {"video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
             "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/mpeg": ".mpg"}


class _HttpError(Exception):
    def __init__(self, status, msg=""):
        Exception.__init__(self, msg)
        self.status = status
        self.msg = msg


REASONS = {200: "OK", 206: "Partial Content", 400: "Bad Request", 401: "Unauthorized",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           416: "Range Not Satisfiable", 500: "Internal Server Error", 503: "Service Unavailable"}


# ---------------------------------------------------------------- engine

class Engine(object):
    def __init__(self, profile, token, log):
        self.profile = profile
        self.token = token
        self._log = log
        self.cfg = {}
        self.loop = None
        self.thread = None
        self.client = None
        self.authorized = False
        self.me = None
        self.phone = None
        self.last_error = ""
        self.server = None
        self.entities = {}
        self.dialog_list = []
        self.dialogs_at = 0
        self.msg_cache = OrderedDict()
        self.index = None
        self._client_lock = None
        self._dialog_lock = None
        self._img_sem = None
        self.cache_dir = os.path.join(profile, "cache")
        for sub in ("thumbs", "photos", "avatars"):
            os.makedirs(os.path.join(self.cache_dir, sub), exist_ok=True)

    # ---------- lifecycle (called from Kodi's service thread)

    def start(self, cfg):
        self.cfg = dict(cfg)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        failure = []

        def run():
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._startup())
            except Exception as e:  # e.g. port already in use
                failure.append(e)
                ready.set()
                return
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, name="tg-engine", daemon=True)
        self.thread.start()
        if not ready.wait(30):
            raise RuntimeError("engine start timed out")
        if failure:
            self.loop = None
            raise failure[0]

    def stop(self):
        if not self.loop:
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            fut.result(15)
        except Exception as e:
            self._log("shutdown error: %r" % e)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    def update_config(self, cfg):
        """Settings changed in Kodi."""
        old = self.cfg
        self.cfg = dict(cfg)
        creds_changed = (old.get("api_id"), old.get("api_hash")) != (cfg.get("api_id"), cfg.get("api_hash"))
        asyncio.run_coroutine_threadsafe(self._apply_config(creds_changed), self.loop)

    async def _apply_config(self, creds_changed):
        if self.index:
            self.index.ttl = self.cfg.get("cache_minutes", 10) * 60
        self.dialogs_at = 0  # include/exclude toggles may have changed
        if creds_changed:
            await self._drop_client()
            await self._ensure_client()

    async def _startup(self):
        self._client_lock = asyncio.Lock()
        self._dialog_lock = asyncio.Lock()
        self._img_sem = asyncio.Semaphore(6)
        self.index = MediaIndex(self._fetch_media, ttl_seconds=self.cfg.get("cache_minutes", 10) * 60,
                                concurrency=4, log=self._log)
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.cfg.get("port", 8765),
                                                 limit=65536)
        self._log("local server listening on 127.0.0.1:%d" % self.cfg.get("port", 8765))
        self.loop.create_task(self._ensure_client())
        self.loop.create_task(self._prune_cache_loop())

    async def _shutdown(self):
        if self.server:
            self.server.close()
        await self._drop_client()
        me = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not me]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # ---------- telegram client

    def _session_path(self):
        return os.path.join(self.profile, SESSION_FILE)

    def _load_session(self):
        try:
            with open(self._session_path(), "r") as f:
                return f.read().strip()
        except (IOError, OSError):
            return ""

    def _save_session(self):
        if not self.client:
            return
        s = self.client.session.save()
        if not s:
            return
        path = self._session_path()
        with open(path, "w") as f:
            f.write(s)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    async def _drop_client(self):
        if self.client:
            try:
                if self.authorized:
                    self._save_session()
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.authorized = False
        self.me = None
        self.entities.clear()
        self.dialog_list = []
        self.dialogs_at = 0
        self.msg_cache.clear()
        if self.index:
            self.index.clear()

    async def _ensure_client(self):
        async with self._client_lock:
            if self.client and self.client.is_connected():
                return self.client
            api_id, api_hash = self.cfg.get("api_id"), self.cfg.get("api_hash")
            if not api_id or not api_hash:
                self.last_error = "API ID / hash not set"
                return None
            if self.client is None:
                self.client = TelegramClient(
                    StringSession(self._load_session()), int(api_id), api_hash,
                    device_model="Kodi", system_version="Kodi add-on", app_version="1.0.0",
                    receive_updates=False, flood_sleep_threshold=120, request_retries=5,
                    connection_retries=5, retry_delay=2)
            try:
                await self.client.connect()
                self.authorized = await self.client.is_user_authorized()
                if self.authorized:
                    self.me = await self.client.get_me()
                    self._save_session()
                self.last_error = ""
            except Exception as e:
                self.last_error = "connect failed: %r" % e
                self._log(self.last_error)
                return None
            return self.client

    async def _authed_client(self):
        c = await self._ensure_client()
        if c is None:
            raise _HttpError(503, self.last_error or "not configured")
        if not self.authorized:
            raise _HttpError(401, "not logged in")
        return c

    # ---------- dialogs / entities

    def _wanted_types(self):
        t = set()
        if self.cfg.get("include_channels", True):
            t.add("channel")
        if self.cfg.get("include_groups", True):
            t.add("group")
        if self.cfg.get("include_private", True):
            t.add("user")
        return t

    async def dialogs(self, refresh=False):
        client = await self._authed_client()
        ttl = self.cfg.get("cache_minutes", 10) * 60
        async with self._dialog_lock:
            if not refresh and self.dialog_list and time.time() - self.dialogs_at < ttl:
                return self.dialog_list
            out = []
            async for d in client.iter_dialogs():
                if d.is_user:
                    typ = "user"
                elif d.is_group:
                    typ = "group"
                elif d.is_channel:
                    typ = "channel"
                else:
                    continue
                self.entities[d.id] = d.input_entity
                ent = d.entity
                title = d.name or ""
                if typ == "user" and getattr(ent, "is_self", False):
                    title = "Saved Messages"
                photo = getattr(ent, "photo", None)
                out.append({
                    "id": d.id,
                    "title": title,
                    "type": typ,
                    "unread": d.unread_count or 0,
                    "has_photo": bool(photo) and not isinstance(
                        photo, (types.ChatPhotoEmpty, types.UserProfilePhotoEmpty)),
                    "archived": bool(d.archived),
                })
            self.dialog_list = out
            self.dialogs_at = time.time()
            self._save_session()
            return out

    async def entity(self, chat_id):
        ent = self.entities.get(chat_id)
        if ent is not None:
            return ent
        await self.dialogs(refresh=True)
        ent = self.entities.get(chat_id)
        if ent is not None:
            return ent
        client = await self._authed_client()
        try:
            ent = await client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            raise _HttpError(404, "unknown chat %s" % chat_id)
        self.entities[chat_id] = ent
        return ent

    def _title(self, chat_id):
        for d in self.dialog_list:
            if d["id"] == chat_id:
                return d["title"]
        return ""

    def _remember(self, chat_id, m):
        self.msg_cache[(chat_id, m.id)] = m
        self.msg_cache.move_to_end((chat_id, m.id))
        while len(self.msg_cache) > 5000:
            self.msg_cache.popitem(last=False)

    async def message(self, chat_id, msg_id):
        m = self.msg_cache.get((chat_id, msg_id))
        if m is not None:
            return m
        client = await self._authed_client()
        ent = await self.entity(chat_id)
        m = await client.get_messages(ent, ids=msg_id)
        if m is None or m.media is None:
            raise _HttpError(404, "message not found")
        self._remember(chat_id, m)
        return m

    async def _fetch_media(self, chat_id, kind, offset_id, limit):
        client = await self._authed_client()
        ent = await self.entity(chat_id)
        msgs = await client.get_messages(ent, limit=limit, offset_id=offset_id, filter=FILTERS[kind])
        title = self._title(chat_id)
        items = []
        for m in msgs:
            it = message_to_item(m, chat_id, title)
            if it and (kind == "all" or it["kind"] == kind):
                items.append(it)
                self._remember(chat_id, m)
        next_off = min((m.id for m in msgs), default=0)
        return items, next_off, len(msgs) < limit

    async def media_page(self, chats, kind, cursor, n):
        if kind not in FILTERS:
            raise _HttpError(400, "bad kind")
        dl = await self.dialogs()
        if chats == "all":
            wanted = self._wanted_types()
            ids = [d["id"] for d in dl if d["type"] in wanted]
        else:
            try:
                ids = [int(x) for x in chats.split(",") if x]
            except ValueError:
                raise _HttpError(400, "bad chats")
        items, nxt = await self.index.page(ids, kind, decode_cursor(cursor), n)
        return {"items": items, "next": nxt}

    # ---------- login

    async def login_send_code(self, phone):
        client = await self._ensure_client()
        if client is None:
            raise _HttpError(503, self.last_error)
        self.phone = phone.strip()
        await client.send_code_request(self.phone)
        return {"ok": True}

    async def login_code(self, code):
        client = await self._ensure_client()
        if client is None:
            raise _HttpError(503, self.last_error)
        try:
            await client.sign_in(self.phone, code.strip())
        except errors.SessionPasswordNeededError:
            return {"ok": False, "need_password": True}
        return await self._login_done()

    async def login_password(self, password):
        client = await self._ensure_client()
        if client is None:
            raise _HttpError(503, self.last_error)
        await client.sign_in(password=password)
        return await self._login_done()

    async def _login_done(self):
        self.authorized = await self.client.is_user_authorized()
        if self.authorized:
            self.me = await self.client.get_me()
            self._save_session()
        return {"ok": self.authorized}

    async def logout(self):
        if self.client and self.authorized:
            try:
                await self.client.log_out()
            except Exception as e:
                self._log("log_out error: %r" % e)
        try:
            os.remove(self._session_path())
        except OSError:
            pass
        self.authorized = False
        await self._drop_client()
        return {"ok": True}

    def status(self):
        from telethon.crypto import aes, libssl
        if aes.cryptg:
            crypto = "cryptg (fast)"
        elif libssl.decrypt_ige:
            crypto = "libssl (fast)"
        else:
            crypto = "pure Python (slow - video may buffer)"
        name = ""
        if self.me:
            name = " ".join(x for x in (self.me.first_name, self.me.last_name) if x)
            if self.me.username:
                name += " (@%s)" % self.me.username
        return {
            "configured": bool(self.cfg.get("api_id") and self.cfg.get("api_hash")),
            "connected": bool(self.client and self.client.is_connected()),
            "authorized": self.authorized,
            "user": name,
            "crypto": crypto,
            "error": self.last_error,
        }

    # ---------- disk cache

    def _cache_path(self, sub, name):
        return os.path.join(self.cache_dir, sub, name)

    async def _cached(self, sub, name, producer):
        path = self._cache_path(sub, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        async with self._img_sem:
            data = await producer()
        if not data:
            raise _HttpError(404, "no image")
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return data

    async def _prune_cache_loop(self):
        while True:
            try:
                self._prune_cache()
            except Exception as e:
                self._log("cache prune error: %r" % e)
            await asyncio.sleep(3600)

    def _prune_cache(self):
        limit = self.cfg.get("cache_mb", 500) * 1024 * 1024
        files = []
        for sub in ("thumbs", "photos", "avatars"):
            d = os.path.join(self.cache_dir, sub)
            for n in os.listdir(d):
                p = os.path.join(d, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        files.sort()
        for mtime, size, p in files:
            if total <= limit:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass

    # ---------- HTTP

    async def _handle(self, reader, writer):
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                    ConnectionError):
                return
            lines = head.decode("latin-1").split("\r\n")
            try:
                method, target, _ = lines[0].split(" ", 2)
            except ValueError:
                return
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body = b""
            clen = int(headers.get("content-length", "0") or 0)
            if 0 < clen <= 65536:
                body = await reader.readexactly(clen)
            try:
                await self._route(method.upper(), target, headers, body, writer)
            except _HttpError as e:
                await self._send_json(writer, {"error": e.msg, "status": e.status}, e.status)
            except errors.RPCError as e:
                await self._send_json(writer, {"error": "%s: %s" % (type(e).__name__, e)}, 400)
            except (ConnectionError, asyncio.CancelledError):
                raise
            except Exception as e:
                self._log("request %s failed: %r" % (target.split("?")[0], e))
                try:
                    await self._send_json(writer, {"error": repr(e)}, 500)
                except Exception:
                    pass
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _write_head(self, writer, status, hdrs):
        out = ["HTTP/1.1 %d %s" % (status, REASONS.get(status, "OK"))]
        hdrs.setdefault("Connection", "close")
        for k, v in hdrs.items():
            out.append("%s: %s" % (k, v))
        writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))

    async def _send_json(self, writer, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self._write_head(writer, status, {"Content-Type": "application/json", "Content-Length": len(data)})
        writer.write(data)
        await writer.drain()

    async def _send_bytes(self, writer, data, ctype, method):
        self._write_head(writer, 200, {"Content-Type": ctype, "Content-Length": len(data),
                                       "Cache-Control": "max-age=86400"})
        if method != "HEAD":
            writer.write(data)
        await writer.drain()

    async def _route(self, method, target, headers, body, writer):
        path, _, query = target.partition("?")
        q = {k: v[0] for k, v in parse_qs(query).items()}
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if not parts or parts[0] != "t" + self.token:
            raise _HttpError(403, "bad token")
        parts = parts[1:]
        if not parts:
            raise _HttpError(404)
        what = parts[0]

        if what == "api":
            return await self._api(method, parts[1:], q, body, writer)
        if method not in ("GET", "HEAD"):
            raise _HttpError(405)

        if what == "stream" and len(parts) >= 3:
            return await self._stream(int(parts[1]), int(parts[2]), headers, method, writer)
        if what == "photo" and len(parts) >= 3:
            cid, mid = int(parts[1]), int(parts[2])
            data = await self._cached("photos", "%d_%d.jpg" % (cid, mid), lambda: self._dl_photo(cid, mid))
            return await self._send_bytes(writer, data, "image/jpeg", method)
        if what == "thumb" and len(parts) >= 3:
            cid, mid = int(parts[1]), int(parts[2].split(".")[0])
            data = await self._cached("thumbs", "%d_%d.jpg" % (cid, mid), lambda: self._dl_thumb(cid, mid))
            return await self._send_bytes(writer, data, "image/jpeg", method)
        if what == "avatar" and len(parts) >= 2:
            cid = int(parts[1].split(".")[0])
            data = await self._cached("avatars", "%d.jpg" % cid, lambda: self._dl_avatar(cid))
            return await self._send_bytes(writer, data, "image/jpeg", method)
        raise _HttpError(404)

    async def _api(self, method, parts, q, body, writer):
        name = "/".join(parts)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            payload = {}
        if name == "status":
            await self._ensure_client()
            res = self.status()
        elif name == "dialogs":
            res = {"dialogs": await self.dialogs(refresh=q.get("refresh") == "1")}
        elif name == "media":
            n = max(10, min(200, int(q.get("n", self.cfg.get("page_size", 50)))))
            res = await self.media_page(q.get("chats", "all"), q.get("kind", "all"), q.get("cursor"), n)
        elif name == "refresh":
            self.dialogs_at = 0
            self.index.clear()
            self.msg_cache.clear()
            res = {"ok": True}
        elif name == "login/send_code" and method == "POST":
            res = await self.login_send_code(payload.get("phone", ""))
        elif name == "login/code" and method == "POST":
            res = await self.login_code(payload.get("code", ""))
        elif name == "login/password" and method == "POST":
            res = await self.login_password(payload.get("password", ""))
        elif name == "logout" and method == "POST":
            res = await self.logout()
        else:
            raise _HttpError(404, "no such api")
        await self._send_json(writer, res)

    # ---------- media download

    async def _with_fresh_ref(self, cid, mid, func):
        """Run func(message); if Telegram says the cached file reference expired,
        re-fetch the message once and retry."""
        m = await self.message(cid, mid)
        try:
            return await func(m)
        except (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError):
            self.msg_cache.pop((cid, mid), None)
            m = await self.message(cid, mid)
            return await func(m)

    async def _dl_photo(self, cid, mid):
        client = await self._authed_client()
        return await self._with_fresh_ref(cid, mid, lambda m: client.download_media(m, file=bytes))

    async def _dl_thumb(self, cid, mid):
        client = await self._authed_client()
        m = await self.message(cid, mid)
        if isinstance(m.media, types.MessageMediaPhoto):
            sizes = m.media.photo.sizes
        elif isinstance(m.media, types.MessageMediaDocument):
            sizes = m.media.document.thumbs
        else:
            sizes = None
        t = pick_thumb_type(sizes)
        if not t:
            return None
        return await self._with_fresh_ref(cid, mid, lambda mm: client.download_media(mm, file=bytes, thumb=t))

    async def _dl_avatar(self, cid):
        client = await self._authed_client()
        ent = await self.entity(cid)
        return await client.download_profile_photo(ent, file=bytes, download_big=False)

    async def _stream(self, cid, mid, headers, method, writer):
        m = await self.message(cid, mid)
        if not isinstance(m.media, types.MessageMediaDocument):
            raise _HttpError(404, "not a video")
        doc = m.media.document
        size = doc.size
        try:
            start, end, partial = parse_range(headers.get("range"), size)
        except ValueError:
            self._write_head(writer, 416, {"Content-Range": "bytes */%d" % size, "Content-Length": 0})
            await writer.drain()
            return
        hdrs = {"Content-Type": doc.mime_type or "video/mp4", "Accept-Ranges": "bytes",
                "Content-Length": end - start + 1}
        if partial:
            hdrs["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        self._write_head(writer, 206 if partial else 200, hdrs)
        if method == "HEAD":
            await writer.drain()
            return
        parallel = max(1, min(8, self.cfg.get("parallel", 4)))
        gen = self._iter_range(m, doc, start, end, parallel)
        try:
            async for piece in gen:
                writer.write(piece)
                await writer.drain()  # raises when Kodi seeks/stops -> download tasks cancelled
        finally:
            await gen.aclose()

    async def _iter_range(self, m, doc, start, end, parallel):
        """Yield the bytes [start, end] of a document, downloading `parallel`
        512 KiB chunks at a time (round-robin strides) to hide request latency."""
        client = self.client
        size = doc.size
        aligned = start - (start % CHUNK)
        n_total = (end + 1 - aligned + CHUNK - 1) // CHUNK
        p = max(1, min(parallel, n_total))
        queues = [asyncio.Queue(maxsize=2) for _ in range(p)]
        msg_data = (m.input_chat, m.id) if m.input_chat else None

        async def producer(i):
            count = (n_total - i + p - 1) // p
            it = client._iter_download(doc, offset=aligned + i * CHUNK, stride=p * CHUNK,
                                       chunk_size=CHUNK, request_size=CHUNK, limit=count,
                                       file_size=size, msg_data=msg_data)
            try:
                async for ch in it:
                    await queues[i].put(bytes(ch))
                await queues[i].put(None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await queues[i].put(e)
            finally:
                if getattr(it, "_sender", None) is not None:
                    try:
                        await it.close()
                    except Exception:
                        pass

        tasks = [asyncio.ensure_future(producer(i)) for i in range(p)]
        skip = start - aligned
        remaining = end - start + 1
        k = 0
        try:
            while remaining > 0:
                piece = await queues[k % p].get()
                if piece is None:
                    break
                if isinstance(piece, Exception):
                    raise piece
                if skip:
                    piece = piece[skip:]
                    skip = 0
                if len(piece) > remaining:
                    piece = piece[:remaining]
                remaining -= len(piece)
                k += 1
                if piece:
                    yield piece
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class KodiLogHandler(logging.Handler):
    def __init__(self, log):
        logging.Handler.__init__(self)
        self._log = log

    def emit(self, record):
        try:
            self._log("%s: %s" % (record.name, record.getMessage()))
        except Exception:
            pass
