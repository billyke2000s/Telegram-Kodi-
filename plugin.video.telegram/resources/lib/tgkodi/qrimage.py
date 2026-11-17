"""Write QR codes (and a plain backdrop) as PNG files Kodi can display.

Uses the bundled pure-Python `qrcode` package only for the module matrix; the PNG
is written here with zlib so nothing else (PIL, pypng) is needed.
"""
import struct
import zlib

import qrcode


def _png(path, rows):
    """rows: list of equal-length bytes, 8-bit greyscale."""
    height, width = len(rows), len(rows[0])

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + r for r in rows)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


def write_qr(text, path, scale=10, border=4):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=border)
    qr.add_data(text)
    qr.make(fit=True)
    rows = []
    for line in qr.get_matrix():  # True = dark module; includes the quiet-zone border
        row = b"".join((b"\x00" if dark else b"\xff") * scale for dark in line)
        rows.extend([row] * scale)
    _png(path, rows)


def write_solid(path, grey=0, size=8):
    _png(path, [bytes([grey]) * size] * size)
