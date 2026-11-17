"""Fast AES-IGE through an OpenSSL libcrypto that is already on the device.

Telethon only finds OpenSSL via ctypes.util.find_library, which fails on Android
(no ldconfig/gcc), so Kodi boxes fall back to pure-Python AES (well under 1 MB/s,
far less on ARM). Kodi itself ships OpenSSL, so we look for it in:

  * find_library("crypto"/"ssl")       - desktop Linux, CoreELEC/LibreELEC
  * common sonames                     - libcrypto.so, libcrypto.so.3, ...
  * Python's own _ssl/_hashlib modules - dlsym also searches their dependencies
  * libraries already mapped into Kodi - /proc/self/maps (Android, Linux)

Every candidate must pass a known-answer self-test against Telethon's pure-Python
implementation before it is used, so a wrong library can only cost speed, never
correctness. Telethon's own ctypes wrapper also copies data byte by byte; these
wrappers pass buffers directly (~50x faster on the same library).
"""
import ctypes
import ctypes.util
import os
import sys

_KEYWORDS = ("crypto", "ssl", "kodi")


def _candidates():
    seen = set()

    def add(x):
        if x and x not in seen:
            seen.add(x)
            return [x]
        return []

    out = []
    for name in ("crypto", "ssl"):
        try:
            out += add(ctypes.util.find_library(name))
        except Exception:
            pass
    for name in ("libcrypto.so", "libcrypto.so.3", "libcrypto.so.1.1", "libssl.so",
                 "libssl.so.3", "libssl.so.1.1", "libkodi.so"):
        out += add(name)
    for mod in ("_ssl", "_hashlib"):
        try:
            f = getattr(__import__(mod), "__file__", None)
        except Exception:
            f = None
        out += add(f)
    try:
        with open("/proc/self/maps", "r") as maps:
            for line in maps:
                path = line.rstrip("\n").split(" ")[-1]
                base = os.path.basename(path).lower()
                if ".so" in base and any(k in base for k in _KEYWORDS):
                    out += add(path)
    except (IOError, OSError):
        pass
    libs = os.environ.get("KODI_ANDROID_LIBS")
    if libs and os.path.isdir(libs):
        for base in sorted(os.listdir(libs)):
            if base.endswith(".so") and any(k in base.lower() for k in _KEYWORDS):
                out += add(os.path.join(libs, base))
    return out


class _AESKey(ctypes.Structure):
    # openssl/aes.h: AES_MAXNR = 14
    _fields_ = [("rd_key", ctypes.c_uint32 * (4 * 15)), ("rounds", ctypes.c_int)]


def _wrap(lib):
    set_enc, set_dec, ige = lib.AES_set_encrypt_key, lib.AES_set_decrypt_key, lib.AES_ige_encrypt
    for f in (set_enc, set_dec):
        f.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        f.restype = ctypes.c_int
    ige.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    ige.restype = None

    def run(data, key, iv, enc):
        data, key = bytes(data), bytes(key)
        k = _AESKey()
        if (set_enc if enc else set_dec)(key, len(key) * 8, ctypes.byref(k)) != 0:
            raise ValueError("bad AES key")
        out = ctypes.create_string_buffer(len(data))
        ivbuf = ctypes.create_string_buffer(bytes(iv), len(iv))  # OpenSSL updates the IV
        ige(data, out, len(data), ctypes.byref(k), ivbuf, 1 if enc else 0)
        return out.raw

    return (lambda d, k, i: run(d, k, i, False)), (lambda d, k, i: run(d, k, i, True))


def _self_test(decrypt, encrypt):
    from telethon.crypto import aes, libssl
    saved = (aes.cryptg, libssl.decrypt_ige, libssl.encrypt_ige)
    aes.cryptg, libssl.decrypt_ige, libssl.encrypt_ige = None, None, None
    try:
        key = bytes(range(32))
        iv = bytes(range(100, 132))
        plain = bytes((i * 7 + 3) & 0xFF for i in range(96))
        ref_ct = aes.AES.encrypt_ige(plain, key, iv)  # pure Python reference
        return (encrypt(plain, key, iv) == ref_ct and decrypt(ref_ct, key, iv) == plain
                and aes.AES.decrypt_ige(ref_ct, key, iv) == plain)
    finally:
        aes.cryptg, libssl.decrypt_ige, libssl.encrypt_ige = saved


def install(log=lambda m: None):
    """Point Telethon at a fast OpenSSL AES-IGE. Returns a description or None."""
    from telethon.crypto import aes, libssl
    if aes.cryptg:
        return "cryptg"
    for cand in _candidates():
        try:
            lib = ctypes.CDLL(cand)
            decrypt, encrypt = _wrap(lib)
            if not _self_test(decrypt, encrypt):
                log("fastcrypto: %s failed self-test" % cand)
                continue
        except (OSError, AttributeError, ValueError, TypeError):
            continue
        libssl.decrypt_ige, libssl.encrypt_ige = decrypt, encrypt
        log("fastcrypto: using OpenSSL from %s" % cand)
        return cand
    log("fastcrypto: no usable OpenSSL found (python %s, %s); using pure Python"
        % (sys.version.split()[0], sys.platform))
    return None
