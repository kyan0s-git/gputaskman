"""Generate tools/assets/gputaskman.ico (no image libraries needed): a GPU chip with load bars.

Run: python tools/make_icon.py. The .ico holds PNG images at 16-256 px, which Windows Vista and later read.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets" / "gputaskman.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)
BG, CHIP, PIN, GREEN, AMBER, RED = (17, 24, 39), (31, 41, 55), (148, 163, 184), (74, 222, 128), (250, 204, 21), (239, 68, 68)


def draw(n: int) -> list[list[tuple[int, int, int, int]]]:
    px = [[(0, 0, 0, 0)] * n for _ in range(n)]

    def rect(x0, y0, x1, y1, color, alpha=255):
        for y in range(max(0, int(y0)), min(n, int(y1))):
            for x in range(max(0, int(x0)), min(n, int(x1))):
                px[y][x] = (*color, alpha)

    r = n * 0.18  # rounded background
    for y in range(n):
        for x in range(n):
            dx = max(r - x - 0.5, 0, x + 0.5 - (n - r))
            dy = max(r - y - 0.5, 0, y + 0.5 - (n - r))
            if dx * dx + dy * dy <= r * r:
                px[y][x] = (*BG, 255)
    m = n * 0.22
    pins = 4 if n >= 32 else 2
    for i in range(pins):  # pins on all four sides
        c = m + (n - 2 * m) * (i + 0.5) / pins
        w = max(1, n * 0.05)
        rect(c - w / 2, n * 0.08, c + w / 2, m, PIN)
        rect(c - w / 2, n - m, c + w / 2, n * 0.92, PIN)
        rect(n * 0.08, c - w / 2, m, c + w / 2, PIN)
        rect(n - m, c - w / 2, n * 0.92, c + w / 2, PIN)
    rect(m, m, n - m, n - m, CHIP)
    inner = n - 2 * m
    bars = ((0.45, GREEN), (0.75, GREEN), (0.6, AMBER), (0.9, RED)) if n >= 24 else ((0.5, GREEN), (0.9, AMBER))
    gap = inner / (len(bars) * 3 + 1)
    for i, (h, color) in enumerate(bars):
        x0 = m + gap + i * 3 * gap
        rect(x0, n - m - gap - h * (inner - 2 * gap), x0 + 2 * gap, n - m - gap, color)
    return px


def png(px) -> bytes:
    n = len(px)
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    images = [png(draw(n)) for n in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for n, data in zip(SIZES, images):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(header + entries + b"".join(images))
    print(OUT)


if __name__ == "__main__":
    main()
