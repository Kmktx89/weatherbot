"""One-shot icon generator for the Weatherbot PWA. Run once: `python _gen_icon.py`.
Produces icon.png (512x512, RGB, stdlib-only — no Pillow dependency)."""

import struct
import zlib

W = H = 512
BG = (0x0f, 0x11, 0x15)
FG = (0x58, 0xa6, 0xff)
ON = (0xff, 0xff, 0xff)


def in_rounded_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    if x < x0 + r and y < y0 + r:
        return (x - (x0 + r)) ** 2 + (y - (y0 + r)) ** 2 <= r * r
    if x > x1 - r and y < y0 + r:
        return (x - (x1 - r)) ** 2 + (y - (y0 + r)) ** 2 <= r * r
    if x < x0 + r and y > y1 - r:
        return (x - (x0 + r)) ** 2 + (y - (y1 - r)) ** 2 <= r * r
    if x > x1 - r and y > y1 - r:
        return (x - (x1 - r)) ** 2 + (y - (y1 - r)) ** 2 <= r * r
    return True


def in_circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


# Layout: blue rounded square inset 64px (12.5% margin keeps content in PWA safe zone).
# Thermometer glyph (white): bulb at (256, 396) r=58, stem 240..272 from y=140 to y=396,
# rounded top cap (circle r=16 at (256, 140)), liquid fill blue inside.
data = bytearray()
for y in range(H):
    data.append(0)
    for x in range(W):
        r, g, b = BG
        if in_rounded_rect(x, y, 64, 64, 448, 448, 80):
            r, g, b = FG
            bulb = in_circle(x, y, 256, 396, 58)
            stem = 240 <= x <= 272 and 140 <= y <= 396
            cap = in_circle(x, y, 256, 140, 16)
            if bulb or stem or cap:
                r, g, b = ON
                # blue liquid: inner bulb (r=40) + inner stem from y=300 to bulb top
                if in_circle(x, y, 256, 396, 40):
                    r, g, b = FG
                elif 246 <= x <= 266 and 300 <= y <= 396:
                    r, g, b = FG
        data.extend([r, g, b])


def chunk(tag, payload):
    crc = zlib.crc32(tag + payload) & 0xffffffff
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(data), 9))
png += chunk(b"IEND", b"")

with open("icon.png", "wb") as f:
    f.write(png)

print(f"wrote icon.png ({len(png)} bytes)")
