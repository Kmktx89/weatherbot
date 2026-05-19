"""One-shot: wrap icon.png into icon.ico (Vista+ PNG-in-ICO format).
Used by the Weatherbot desktop shortcut."""

import struct

with open("icon.png", "rb") as f:
    png = f.read()

ico = struct.pack("<HHH", 0, 1, 1)
ico += struct.pack("<BBBBHHII",
                   0, 0,
                   0, 0,
                   1, 32,
                   len(png),
                   6 + 16)
ico += png

with open("icon.ico", "wb") as f:
    f.write(ico)

print(f"wrote icon.ico ({len(ico)} bytes)")
