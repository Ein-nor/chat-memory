#!/usr/bin/env python3
"""Draws the chat-memory icon (chat bubble + magnifier) as a 1024 px RGBA PNG.

Pure standard library: shapes are signed distance functions, the edge
coverage gives anti-aliasing. Takes about 7 seconds.

Usage:
  python3 scripts/draw_icon.py [OUTPUT]   # default: .claude-plugin/icon.png
"""
import math
import os
import struct
import sys
import zlib

N = 1024


def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def cov(d):  # distance -> pixel coverage
    return clamp(0.5 - d)


def rbox(px, py, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(px - cx) - hx, abs(py - cy) - hy
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def segment(px, py, ax, ay, bx, by, r):
    vx, vy = bx - ax, by - ay
    t = clamp(((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy))
    return math.hypot(px - ax - t * vx, py - ay - t * vy) - r


def triangle(px, py, pts):
    # signed distance to a polygon (Inigo Quilez)
    d = float("inf")
    s = 1.0
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[i - 1]
        ex, ey = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        t = clamp((wx * ex + wy * ey) / (ex * ex + ey * ey))
        d = min(d, math.hypot(wx - ex * t, wy - ey * t))
        c1, c2, c3 = py >= ay, py < by, ex * wy > ey * wx
        if (c1 and c2 and c3) or (not c1 and not c2 and not c3):
            s = -s
    return s * d


def hexrgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


TOP, BOTTOM = hexrgb("#4F46E5"), hexrgb("#7C3AED")
WHITE = (1.0, 1.0, 1.0)
LINE = hexrgb("#C7D2FE")
AMBER = hexrgb("#F59E0B")
LENS = hexrgb("#FFF8E6")

# geometry
BUBBLE = (170, 210, 790, 650, 120)
TAIL = [(270, 600), (250, 820), (450, 640)]
LINES = [(270, 340, 640), (270, 430, 690), (270, 520, 520)]  # x0, y, x1
RING_C, RING_R, RING_W = (690, 650), 165, 50
HANDLE = (790, 750, 900, 860, 44)


def over(dst, color, a):
    return tuple(d * (1 - a) + c * a for d, c in zip(dst, color)), a


def pixel(px, py):
    bg = cov(rbox(px, py, 0, 0, N, N, 230))
    if bg <= 0:
        return (0, 0, 0, 0)
    t = py / N
    col = tuple(a * (1 - t) + b * t for a, b in zip(TOP, BOTTOM))

    a = max(cov(rbox(px, py, *BUBBLE)), cov(triangle(px, py, TAIL)))
    if a:
        col, _ = over(col, WHITE, a)
    for x0, y, x1 in LINES:
        a = cov(segment(px, py, x0, y, x1, y, 22))
        if a:
            col, _ = over(col, LINE, a)

    dc = circle(px, py, RING_C[0], RING_C[1], RING_R)
    # dark halo so the magnifier separates from the bubble
    halo = cov(abs(dc) - RING_W / 2 - 18)
    halo = max(halo, cov(segment(px, py, *HANDLE[:4], HANDLE[4] + 18)))
    if halo:
        col, _ = over(col, BOTTOM, halo)
    ring = max(cov(abs(dc) - RING_W / 2), cov(segment(px, py, *HANDLE)))
    if ring:
        col, _ = over(col, AMBER, ring)
    # glass last, so the handle's inner end stays hidden under it
    lens = cov(dc + RING_W / 2)
    if lens:
        col, _ = over(col, LENS, lens)
    # small highlight on the lens
    hl = cov(segment(px, py, 610, 590, 640, 560, 14))
    if hl:
        col, _ = over(col, WHITE, hl * 0.9)
    return tuple(round(c * 255) for c in col) + (round(bg * 255),)


def write_png(path, rows):
    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)


def main():
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "icon.png")
    out = sys.argv[1] if len(sys.argv) > 1 else default
    rows = [[pixel(x + 0.5, y + 0.5) for x in range(N)] for y in range(N)]
    write_png(out, rows)
    print("Wrote", os.path.normpath(out))


if __name__ == "__main__":
    main()
