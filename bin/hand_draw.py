#!/usr/bin/env python3
"""Hand-drawn annotation marks for the art engine.

Nate's critique (Oct 4 2026): the programmatic circle "looked weird."
A good hand-drawn circle is NOT a perfect ellipse — it wobbles, the stroke
weight varies, and the pen often double-tracks slightly where the hand
corrects. This module draws marks that read as drawn, not plotted.
"""
import math, random
from PIL import Image, ImageDraw


def hand_circle(draw, cx, cy, rx, ry, color="#ff2e88", width=10,
                wobble=0.035, seed=7, rotation_deg=-8):
    """Draw a wobbly hand-drawn circle (ellipse) on a PIL ImageDraw."""
    rnd = random.Random(seed)
    p1 = rnd.uniform(0, math.pi * 2)
    p2 = rnd.uniform(0, math.pi * 2)
    rot = math.radians(rotation_deg)
    for pass_i, (lw, alpha_off) in enumerate([(width, 0), (max(2, width - 4), 3)]):
        pts = []
        steps = 140
        # second pass doesn't quite close — the pen lifts early
        end = steps if pass_i == 0 else int(steps * 0.92)
        for i in range(end + 1):
            t = 2 * math.pi * i / steps
            r = 1 + wobble * math.sin(3 * t + p1) + wobble * 0.6 * math.sin(7 * t + p2)
            x = rx * r * math.cos(t)
            y = ry * r * math.sin(t)
            xr = x * math.cos(rot) - y * math.sin(rot)
            yr = x * math.sin(rot) + y * math.cos(rot)
            pts.append((cx + xr + alpha_off, cy + yr + alpha_off))
        draw.line(pts, fill=color, width=lw, joint="curve")


def hand_arrow(draw, x1, y1, x2, y2, color="#ff2e88", width=9, seed=3):
    """Slightly bowed hand-drawn arrow."""
    rnd = random.Random(seed)
    mx, my = (x1 + x2) / 2 + rnd.uniform(-14, 14), (y1 + y2) / 2 + rnd.uniform(-14, 14)
    pts = []
    for i in range(41):
        t = i / 40
        x = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * mx + t ** 2 * x2
        y = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * my + t ** 2 * y2
        pts.append((x, y))
    draw.line(pts, fill=color, width=width, joint="curve")
    # head: two short strokes
    ang = math.atan2(y2 - my, x2 - mx)
    hl = 34
    for da in (0.5, -0.5):
        hx = x2 - hl * math.cos(ang + da)
        hy = y2 - hl * math.sin(ang + da)
        draw.line([(x2, y2), (hx, hy)], fill=color, width=width, joint="curve")


def hand_underline(draw, x1, x2, y, color="#ff2e88", width=9, seed=11):
    rnd = random.Random(seed)
    pts = [(x1 + (x2 - x1) * i / 60,
            y + 6 * math.sin(i / 60 * math.pi * 2 + rnd.random()) + rnd.uniform(-2, 2))
           for i in range(61)]
    draw.line(pts, fill=color, width=width, joint="curve")


if __name__ == "__main__":
    # demo: refined circle on the shirt frame
    base = Image.open("/tmp/base.png").convert("RGB")  # post on black canvas
    d = ImageDraw.Draw(base)
    hand_circle(d, 540, 960, 300, 300)
    base.save("/tmp/annotation-demo.png")
    print("wrote /tmp/annotation-demo.png")
