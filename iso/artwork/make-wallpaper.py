#!/usr/bin/env python3
"""Draw the live desktop's wallpaper, "Indigo silk": an indigo night with a bundle of violet strands of light.

    make-wallpaper.py OUTPUT.svg

The picture is original artwork made for this disc (no Gentoo logo: the Gentoo name and logo guidelines do not allow
it on a product that is not official). It uses gradients and strokes only, no filters, so librsvg draws it at 4K in
well under a second, and the SVG fits every screen size. Its violet goes with the installer's accent (theme.ACCENT).
The output is fixed: tests/iso/test_artwork.py checks that the committed file is what this script draws.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

WIDTH, HEIGHT = 3840, 2160
STRANDS = 22
# (width, opacity) of the wide, faint strokes under the bundle; together they fall off like a soft glow
GLOW = ((900, 0.012), (720, 0.014), (560, 0.016), (420, 0.018), (300, 0.022), (200, 0.026), (120, 0.03), (60, 0.035))


def strand(index: int, count: int = STRANDS) -> tuple[str, float, float]:
    """Path, stroke width and opacity of one strand: a curve from the lower left to the upper right, widest in the middle of the bundle."""
    spread = index / (count - 1) - 0.5
    y0 = 1780 + spread * 520 + 90 * math.sin(index * 1.7)
    y1 = 1350 + spread * 260 + 60 * math.sin(index * 2.3)
    y2 = 820 + spread * 180 + 50 * math.cos(index * 1.3)
    y3 = 420 + spread * 380 + 70 * math.sin(index * 0.9)
    path = (f"M -300 {y0:.0f} C 700 {y0 - 260 - 140 * spread:.0f}, 1500 {y1 + 120:.0f}, 2150 {y1 - 120:.0f} "
            f"S 3300 {y2 - 60 * spread:.0f}, 4150 {y3:.0f}")
    width = 2 + 26 * (1 - abs(spread) * 1.7) ** 2 if abs(spread) < 0.58 else 2
    opacity = 0.05 + 0.21 * (1 - abs(spread) * 1.6) if abs(spread) < 0.62 else 0.05
    return path, width, opacity


def render() -> str:
    centre = strand(STRANDS // 2)[0]
    glow = "\n".join(f'    <path d="{centre}" stroke-width="{width}" stroke-opacity="{opacity}"/>' for width, opacity in GLOW)
    strands = []
    for index in range(STRANDS):
        path, width, opacity = strand(index)
        gradient = "silkA" if index % 3 else "silkB"
        strands.append(f'    <path d="{path}" stroke="url(#{gradient})" stroke-width="{width:.1f}" stroke-opacity="{opacity:.3f}" stroke-linecap="round"/>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Indigo silk: the wallpaper of the Gentoo installer live disc. Drawn by iso/artwork/make-wallpaper.py; edit that, not this file. -->
<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="xMidYMid slice">
  <title>Indigo silk</title>
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0c0b1a"/>
      <stop offset="0.5" stop-color="#16132e"/>
      <stop offset="1" stop-color="#1f1840"/>
    </linearGradient>
    <radialGradient id="glow" cx="2650" cy="820" r="2500" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#7d64d8" stop-opacity="0.42"/>
      <stop offset="0.3" stop-color="#5a46a6" stop-opacity="0.22"/>
      <stop offset="0.7" stop-color="#2a2160" stop-opacity="0.06"/>
      <stop offset="1" stop-color="#16132e" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="deep" cx="300" cy="2300" r="2100" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#2a5fb0" stop-opacity="0.34"/>
      <stop offset="1" stop-color="#2a5fb0" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="silkA" x1="-300" y1="0" x2="4150" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#7f9cff" stop-opacity="0"/>
      <stop offset="0.25" stop-color="#8fa4ff"/>
      <stop offset="0.6" stop-color="#b6a4ff"/>
      <stop offset="1" stop-color="#d9ccff" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="silkB" x1="-300" y1="0" x2="4150" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#c9bbff" stop-opacity="0"/>
      <stop offset="0.45" stop-color="#e4dcff"/>
      <stop offset="0.85" stop-color="#a48cff"/>
      <stop offset="1" stop-color="#a48cff" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="vignette" cx="1920" cy="1080" r="2350" gradientUnits="userSpaceOnUse">
      <stop offset="0.5" stop-color="#04030a" stop-opacity="0"/>
      <stop offset="1" stop-color="#04030a" stop-opacity="0.62"/>
    </radialGradient>
  </defs>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#sky)"/>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#glow)"/>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#deep)"/>
  <g fill="none" stroke="url(#silkA)" stroke-linecap="round">
{glow}
  </g>
  <g fill="none">
{chr(10).join(strands)}
  </g>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#vignette)"/>
</svg>
"""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: make-wallpaper.py OUTPUT.svg", file=sys.stderr)
        return 2
    Path(argv[0]).write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
