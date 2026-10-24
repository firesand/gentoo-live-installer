"""The live desktop's wallpaper (iso/artwork): drawn by a script, quick to render, nothing outside the file."""
from __future__ import annotations

import shutil
import subprocess
from xml.etree import ElementTree

import pytest

from .conftest import ROOT, load_script

wallpaper = load_script("iso/artwork/make-wallpaper.py", "make_wallpaper")
SVG = ROOT / "iso/root_overlay/usr/share/backgrounds/gentoo-live/indigo-silk.svg"
NS = "{http://www.w3.org/2000/svg}"


def test_the_committed_wallpaper_is_what_the_script_draws() -> None:
    assert SVG.read_text(encoding="utf-8") == wallpaper.render(), "run iso/artwork/make-wallpaper.py " + str(SVG.relative_to(ROOT))


def test_the_wallpaper_is_a_self_contained_4k_svg_without_filters() -> None:
    """No filter (slow to draw at 4K on every login), no image or link to another file, and a 16:9 picture that fills any screen."""
    svg = ElementTree.parse(SVG).getroot()
    assert svg.tag == f"{NS}svg" and svg.get("viewBox") == "0 0 3840 2160" and svg.get("preserveAspectRatio") == "xMidYMid slice"
    tags = {element.tag.removeprefix(NS) for element in svg.iter()}
    assert not tags & {"filter", "image", "use", "script", "foreignObject", "a"}
    assert "href" not in SVG.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("rsvg-convert") is None, reason="rsvg-convert (librsvg, the live system's SVG loader) is not installed")
@pytest.mark.parametrize("size", [(1366, 768), (1920, 1080), (3840, 2160)])
def test_librsvg_draws_it_at_common_screen_sizes(size: tuple[int, int]) -> None:
    width, height = size
    png = subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height), str(SVG)], capture_output=True, check=True, timeout=30).stdout
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == width and int.from_bytes(png[20:24], "big") == height
