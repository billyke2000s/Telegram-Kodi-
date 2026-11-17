/* AES-IGE chaining in C around an AES single-block function supplied by the caller
 * (e.g. OpenSSL's AES_encrypt/AES_decrypt found inside Kodi's own libkodi.so).
 *
 * Kodi on Android exposes AES_encrypt/AES_decrypt but not AES_ige_encrypt, so doing
 * the chaining in Python costs one ctypes call per 16 bytes. This does it in C.
 *
 * Freestanding: no libc, no imports - the only external code it runs is `fn`.
 * Build: tools/native/build.sh
 */
typedef void (*aes_block_fn)(const unsigned char *in, unsigned char *out, const void *key);

__attribute__((visibility("default")))
int tg_ige(const unsigned char *in, unsigned char *out, unsigned long len,
           const void *key, const unsigned char *iv32, int enc, aes_block_fn fn)
{
    unsigned char iv1[16], iv2[16], x[16], y[16], cur[16];
    unsigned long off;
    int i;
    if (len % 16)
        return -1;
    for (i = 0; i < 16; i++) {
        iv1[i] = iv32[i];
        iv2[i] = iv32[16 + i];
    }
    for (off = 0; off < len; off += 16) {
        for (i = 0; i < 16; i++)
            cur[i] = in[off + i];          /* safe if in == out */
        if (enc) {                         /* c = E(p ^ iv1) ^ iv2; iv1 = c; iv2 = p */
            for (i = 0; i < 16; i++) x[i] = cur[i] ^ iv1[i];
            fn(x, y, key);
            for (i = 0; i < 16; i++) { y[i] ^= iv2[i]; iv1[i] = y[i]; iv2[i] = cur[i]; }
        } else {                           /* p = D(c ^ iv2) ^ iv1; iv1 = c; iv2 = p */
            for (i = 0; i < 16; i++) x[i] = cur[i] ^ iv2[i];
            fn(x, y, key);
            for (i = 0; i < 16; i++) { y[i] ^= iv1[i]; iv1[i] = cur[i]; iv2[i] = y[i]; }
        }
        for (i = 0; i < 16; i++)
            out[off + i] = y[i];
    }
    return 0;
}

__attribute__((visibility("default")))
int tg_ige_version(void) { return 1; }
