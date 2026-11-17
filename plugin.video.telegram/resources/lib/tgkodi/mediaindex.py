"""Merged, paginated media listing across one or many chats.

Each (chat, kind) pair keeps a buffer of media messages fetched newest-first.
A page is built by making sure every chat's buffer holds at least `n` items older
than the cursor (or is exhausted), then merging and taking the newest `n`.
That makes the combined Images/Videos/Mixed listings exact (nothing skipped,
nothing duplicated) without re-fetching every chat on every page.

This module has no Kodi or Telethon imports so it can be unit-tested on its own:
the caller supplies `fetch(chat_id, kind, offset_id, limit) -> (items, next_offset_id, exhausted)`,
where next_offset_id is the lowest raw message id seen (so filtered-out messages are
never fetched twice).
"""
import asyncio
import time


def item_key(item):
    return (item["date"], item["chat_id"], item["msg_id"])


def encode_cursor(key):
    return "%d_%d_%d" % key


def decode_cursor(text):
    if not text:
        return None
    a, b, c = text.split("_")
    return (int(a), int(b), int(c))


class _Buffer(object):
    __slots__ = ("items", "exhausted", "next_offset_id", "created", "lock")

    def __init__(self):
        self.items = []          # newest first
        self.exhausted = False
        self.next_offset_id = 0  # 0 = start from newest message
        self.created = time.time()
        self.lock = asyncio.Lock()


class MediaIndex(object):
    def __init__(self, fetch, ttl_seconds=600, concurrency=4, log=None):
        self._fetch = fetch
        self.ttl = ttl_seconds
        self._sem = asyncio.Semaphore(concurrency)
        self._bufs = {}
        self._log = log or (lambda m: None)

    def clear(self):
        self._bufs.clear()

    @staticmethod
    def _older_count(buf, cursor):
        if cursor is None:
            return len(buf.items)
        return sum(1 for it in buf.items if item_key(it) < cursor)

    async def _fill(self, chat_id, kind, cursor, n):
        key = (chat_id, kind)
        buf = self._bufs.get(key)
        if buf is None:
            buf = self._bufs[key] = _Buffer()
        async with buf.lock:
            while not buf.exhausted and self._older_count(buf, cursor) < n:
                want = min(100, max(20, n))
                async with self._sem:
                    try:
                        items, next_off, exhausted = await self._fetch(
                            chat_id, kind, buf.next_offset_id, want)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # private/banned/deleted chat etc.
                        self._log("fetch failed for chat %s (%s): %r" % (chat_id, kind, e))
                        buf.exhausted = True
                        break
                buf.items.extend(items)
                progressed = bool(next_off) and (buf.next_offset_id == 0 or next_off < buf.next_offset_id)
                if progressed:
                    buf.next_offset_id = next_off
                buf.exhausted = exhausted or not progressed
        return buf

    async def page(self, chat_ids, kind, cursor=None, n=50, latest=None, batch=8):
        """Return (items, next_cursor_text_or_None).

        latest: optional {chat_id: unix time of the chat's newest message}. A chat can't
        hold media newer than that, so chats are visited newest-activity first and the
        walk stops once no remaining chat could beat the n-th item already collected.
        The result is identical to visiting every chat, just far fewer requests when
        there are many chats. Chats without a known time are always visited.
        """
        now = time.time()
        if cursor is None:
            # Fresh browse from the top: drop stale buffers so new posts show up.
            for cid in chat_ids:
                b = self._bufs.get((cid, kind))
                if b is not None and now - b.created > self.ttl:
                    del self._bufs[(cid, kind)]
        latest = latest or {}
        inf = float("inf")
        order = sorted(chat_ids, key=lambda c: latest.get(c, inf), reverse=True)
        pool = []
        more = False
        i = 0
        while i < len(order):
            if len(pool) >= n:
                pool.sort(key=item_key, reverse=True)
                nth_date = pool[n - 1]["date"]
                # Remaining chats are sorted, so if the next can't beat the n-th item none can.
                if latest.get(order[i], inf) < nth_date:
                    more = True  # skipped chats may still have older items for later pages
                    break
            chunk = order[i:i + batch]
            i += batch
            bufs = await asyncio.gather(*[self._fill(cid, kind, cursor, n) for cid in chunk])
            for buf in bufs:
                pool.extend(it for it in buf.items if cursor is None or item_key(it) < cursor)
                if not buf.exhausted:
                    more = True
        pool.sort(key=item_key, reverse=True)
        page = pool[:n]
        if len(pool) > n:
            more = True
        nxt = encode_cursor(item_key(page[-1])) if (page and more) else None
        return page, nxt
