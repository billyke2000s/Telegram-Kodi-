"""Fast AES-IGE through an OpenSSL libcrypto that is already on the device.

Telethon only finds OpenSSL via ctypes.util.find_library, which fails on Android
(no ldconfig/gcc), so Kodi boxes fall back to pure-Python AES (well under 1 MB/s,
far less on ARM). Kodi itself ships OpenSSL, so we look for it in:

  * find_library("crypto"/"ssl")       - desktop Linux, CoreELEC/LibreELEC
  * common sonames                     - libcrypto.so, libcrypto.so.3, ...
  * Python's own _ssl/_hashlib modules - dlsym also searches their dependencies
  * libraries already mapped into Kodi - /proc/self/maps (Android, Linux)

Three ways of using it, best first, tried across all candidates in that order:

  ige    - AES_ige_encrypt does the whole buffer in C           (tens of MB/s)
  native - bundled tiny C helper (_native/ige/<arch>/libtgige.so) does the IGE chaining
           and calls the library's AES_encrypt/AES_decrypt per block (almost as fast).
           Kodi on Android has AES_encrypt/AES_decrypt in libkodi.so but no IGE.
  block  - AES_encrypt/AES_decrypt per 16-byte block, IGE chaining in Python
  evp   - EVP AES-ECB per block (always present where TLS works), chaining in Python

"block"/"evp" are roughly 10x faster than pure Python. Every candidate must pass a
known-answer self-test against Telethon's pure-Python implementation before it is
used, so a wrong library can only cost speed, never correctness.
"""
import ctypes
import ctypes.util
import hashlib
import os
import platform
import shutil
import struct
import sys

_NATIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_native", "ige")

ACTIVE = None  # human-readable description of what is in use, for the status screen

_PREFERRED = ("crypto", "ssl")
_KEYWORDS = ("crypto", "ssl", "kodi", "python", "hashlib")


def _mapped_libs():
    out = []
    try:
        with open("/proc/self/maps", "r") as maps:
            for line in maps:
                path = line.rstrip("\n").split(" ")[-1]
                if path.startswith("/") and ".so" in os.path.basename(path) and path not in out:
                    out.append(path)
    except (IOError, OSError):
        pass
    return out


def _candidates():
    seen, out = set(), []

    def add(x):
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    for name in ("crypto", "ssl"):
        try:
            add(ctypes.util.find_library(name))
        except Exception:
            pass
    for name in ("libcrypto.so", "libcrypto.so.3", "libcrypto.so.1.1", "libssl.so",
                 "libssl.so.3", "libssl.so.1.1", "libkodi.so"):
        add(name)
    for mod in ("_ssl", "_hashlib"):
        try:
            add(getattr(__import__(mod), "__file__", None))
        except Exception:
            pass
    mapped = _mapped_libs()
    libs = os.environ.get("KODI_ANDROID_LIBS")
    extra = []
    if libs and os.path.isdir(libs):
        extra = [os.path.join(libs, b) for b in sorted(os.listdir(libs)) if b.endswith(".so")]
    # likely names first (mapped, then Kodi's own lib dir), then every other library
    # Kodi has ALREADY loaded - re-opening those is harmless; we never load unrelated new ones
    for kw_list in (_PREFERRED, _KEYWORDS):
        for p in mapped + extra:
            if any(k in os.path.basename(p).lower() for k in kw_list):
                add(p)
    for p in mapped:
        add(p)
    return out


def _aes_key_buf():
    # AES_KEY is 244 bytes, or 488 where OpenSSL was built with AES_LONG (8-byte words).
    # We never read it, so give OpenSSL plenty of room either way.
    return ctypes.create_string_buffer(1024)


def _proto(f, args, res):
    f.argtypes, f.restype = args, res
    return f


def _ige_full(lib):
    set_enc = _proto(lib.AES_set_encrypt_key, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    set_dec = _proto(lib.AES_set_decrypt_key, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    ige = _proto(lib.AES_ige_encrypt, [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int], None)

    def run(data, key, iv, enc):
        data, key = bytes(data), bytes(key)
        k = _aes_key_buf()
        if (set_enc if enc else set_dec)(key, len(key) * 8, k) != 0:
            raise ValueError("bad AES key")
        out = ctypes.create_string_buffer(len(data))
        ivbuf = ctypes.create_string_buffer(bytes(iv), len(iv))  # OpenSSL updates the IV
        ige(data, out, len(data), k, ivbuf, 1 if enc else 0)
        return out.raw
    return run


def _chain(data, iv, enc, blk):
    """IGE chaining around a single-block AES function blk(16 bytes) -> 16 bytes."""
    fb = int.from_bytes
    data = bytes(data)
    a, b = fb(iv[:16], "big"), fb(iv[16:32], "big")  # iv1, iv2
    out = bytearray(len(data))
    for off in range(0, len(data) - 15, 16):
        x = fb(data[off:off + 16], "big")
        if enc:   # x = plain:  c = E(p ^ iv1) ^ iv2 ; iv1 = c ; iv2 = p
            y = fb(blk((x ^ a).to_bytes(16, "big")), "big") ^ b
            a, b = y, x
        else:     # x = cipher: p = D(c ^ iv2) ^ iv1 ; iv1 = c ; iv2 = p
            y = fb(blk((x ^ b).to_bytes(16, "big")), "big") ^ a
            a, b = x, y
        out[off:off + 16] = y.to_bytes(16, "big")
    return bytes(out)


def _ige_block(lib):
    set_enc = _proto(lib.AES_set_encrypt_key, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    set_dec = _proto(lib.AES_set_decrypt_key, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    e = _proto(lib.AES_encrypt, [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p], None)
    d = _proto(lib.AES_decrypt, [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p], None)

    def run(data, key, iv, enc):
        key = bytes(key)
        k = _aes_key_buf()
        if (set_enc if enc else set_dec)(key, len(key) * 8, k) != 0:
            raise ValueError("bad AES key")
        kref, outb, f = k, ctypes.create_string_buffer(16), (e if enc else d)

        def blk(x):
            f(x, outb, kref)
            return outb.raw
        return _chain(data, iv, enc, blk)
    return run


def _ige_evp(lib):
    new = _proto(lib.EVP_CIPHER_CTX_new, [], ctypes.c_void_p)
    free = _proto(lib.EVP_CIPHER_CTX_free, [ctypes.c_void_p], None)
    init = _proto(lib.EVP_CipherInit_ex, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int)
    pad = _proto(lib.EVP_CIPHER_CTX_set_padding, [ctypes.c_void_p, ctypes.c_int], ctypes.c_int)
    upd = _proto(lib.EVP_CipherUpdate, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                        ctypes.c_char_p, ctypes.c_int], ctypes.c_int)
    ciphers = {16: lib.EVP_aes_128_ecb, 24: lib.EVP_aes_192_ecb, 32: lib.EVP_aes_256_ecb}
    for c in ciphers.values():
        _proto(c, [], ctypes.c_void_p)

    def run(data, key, iv, enc):
        key = bytes(key)
        ctx = new()
        if not ctx:
            raise MemoryError("EVP_CIPHER_CTX_new failed")
        try:
            if init(ctx, ciphers[len(key)](), None, key, None, 1 if enc else 0) != 1:
                raise ValueError("EVP init failed")
            pad(ctx, 0)
            outb, outl = ctypes.create_string_buffer(32), ctypes.c_int(0)
            ol = ctypes.byref(outl)

            def blk(x):
                upd(ctx, outb, ol, x, 16)
                return outb.raw[:16]
            return _chain(data, iv, enc, blk)
        finally:
            free(ctx)
    return run


def _arch():
    bits = struct.calcsize("P") * 8  # the Kodi process, not the kernel (32-bit Kodi on 64-bit boxes)
    m = platform.machine().lower()
    if m.startswith(("arm", "aarch")):
        return "arm64" if bits == 64 else "arm"
    if m in ("x86_64", "amd64", "i386", "i686", "x86"):
        return "x86_64" if bits == 64 else "x86"
    return None


def _private_dirs():
    """App-private, executable locations. Android mounts shared storage (where Kodi's
    add-ons usually live) noexec, so the helper may need copying somewhere internal."""
    out = []

    def add(d):
        if d and d.startswith("/data/") and d not in out:
            out.append(d)
    paths = []
    try:
        with open("/proc/self/maps", "r") as maps:
            paths = [ln.rstrip("\n").split(" ")[-1] for ln in maps]
    except (IOError, OSError):
        pass
    for p in paths:
        parts = p.split("/")
        if p.startswith("/data/user/") and len(parts) > 4:      # /data/user/0/<pkg>/...
            add("/".join(parts[:5]) + "/cache")
            add("/".join(parts[:5]) + "/files")
        elif p.startswith("/data/data/") and len(parts) > 3:    # /data/data/<pkg>/...
            add("/".join(parts[:4]) + "/cache")
        elif p.startswith("/data/app/") and p.endswith("/libkodi.so"):  # .../<pkg>-<hash>/lib/..
            for seg in parts:
                if "-" in seg and "." in seg.split("-")[0]:
                    add("/data/data/%s/cache" % seg.split("-")[0])
    for d in (os.path.dirname(os.__file__), os.environ.get("TMPDIR"), os.environ.get("HOME")):
        add(d)
    return out


_helper_cache = []


def _helper(extra_dirs=()):
    """Load the bundled IGE helper, copying it to a private dir if needed. None if impossible."""
    if _helper_cache:
        return _helper_cache[0]
    lib = None
    arch = _arch()
    src = os.path.join(_NATIVE_DIR, arch or "-", "libtgige.so")
    if arch and os.path.exists(src):
        try:
            lib = ctypes.CDLL(src)
        except OSError:
            data = open(src, "rb").read()
            name = "libtgige-%s.so" % hashlib.md5(data).hexdigest()[:12]
            for d in list(extra_dirs) + _private_dirs():
                try:
                    dst_dir = os.path.join(d, "tgkodi_native")
                    os.makedirs(dst_dir, exist_ok=True)
                    dst = os.path.join(dst_dir, name)
                    if not os.path.exists(dst):
                        shutil.copyfile(src, dst + ".tmp")
                        os.chmod(dst + ".tmp", 0o755)
                        os.replace(dst + ".tmp", dst)
                    lib = ctypes.CDLL(dst)
                    break
                except OSError:
                    continue
    if lib is not None:
        try:
            if lib.tg_ige_version() != 1:
                lib = None
        except AttributeError:
            lib = None
    _helper_cache.append(lib)
    return lib


def _ige_native(lib):
    helper = _helper()
    if helper is None:
        raise OSError("no IGE helper for this platform")
    set_enc = _proto(lib.AES_set_encrypt_key, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    set_dec = _proto(lib.AES_set_decrypt_key, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    fn_enc = ctypes.cast(lib.AES_encrypt, ctypes.c_void_p).value
    fn_dec = ctypes.cast(lib.AES_decrypt, ctypes.c_void_p).value
    if not fn_enc or not fn_dec:
        raise AttributeError("AES_encrypt/AES_decrypt missing")
    tg = _proto(helper.tg_ige, [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)

    def run(data, key, iv, enc):
        data, key = bytes(data), bytes(key)
        k = _aes_key_buf()
        if (set_enc if enc else set_dec)(key, len(key) * 8, k) != 0:
            raise ValueError("bad AES key")
        out = ctypes.create_string_buffer(len(data))
        if tg(data, out, len(data), k, bytes(iv), 1 if enc else 0, fn_enc if enc else fn_dec) != 0:
            raise ValueError("IGE length must be a multiple of 16")
        return out.raw
    return run


_TIERS = (("ige", _ige_full), ("native", _ige_native), ("block", _ige_block), ("evp", _ige_evp))


def _self_test(run):
    from telethon.crypto import aes, libssl
    saved = (aes.cryptg, libssl.decrypt_ige, libssl.encrypt_ige)
    aes.cryptg, libssl.decrypt_ige, libssl.encrypt_ige = None, None, None
    try:
        key = bytes(range(32))
        iv = bytes(range(100, 132))
        plain = bytes((i * 7 + 3) & 0xFF for i in range(96))
        ref_ct = aes.AES.encrypt_ige(plain, key, iv)  # pure Python reference
        return run(plain, key, iv, True) == ref_ct and run(ref_ct, key, iv, False) == plain
    finally:
        aes.cryptg, libssl.decrypt_ige, libssl.encrypt_ige = saved


def install(log=lambda m: None):
    """Point Telethon at the fastest working AES-IGE. Returns a description or None."""
    global ACTIVE
    from telethon.crypto import aes, libssl
    if aes.cryptg:
        ACTIVE = "cryptg (fast)"
        return ACTIVE
    cands = _candidates()
    handles = {}
    for tier, make in _TIERS:
        for cand in cands:
            try:
                lib = handles.get(cand) or handles.setdefault(cand, ctypes.CDLL(cand))
                run = make(lib)
                if not _self_test(run):
                    log("fastcrypto: %s (%s) failed self-test" % (cand, tier))
                    continue
            except (OSError, AttributeError, ValueError, TypeError, KeyError, MemoryError):
                continue
            libssl.decrypt_ige = lambda d, k, i, _r=run: _r(d, k, i, False)
            libssl.encrypt_ige = lambda d, k, i, _r=run: _r(d, k, i, True)
            ACTIVE = "OpenSSL %s (%s)%s" % (tier, os.path.basename(cand),
                                            " - fast" if tier in ("ige", "native") else " - medium speed")
            log("fastcrypto: using %s" % ACTIVE)
            return ACTIVE
    ACTIVE = None
    log("fastcrypto: no usable OpenSSL among %d libraries (python %s, %s); using pure Python"
        % (len(cands), sys.version.split()[0], sys.platform))
    return None
