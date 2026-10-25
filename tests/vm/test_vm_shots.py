"""Screenshots of the live desktop and of every installer page (scripts/vm_shots.py, scripts/vm-gui-shots.py, the screenshots scenario)."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

import pytest

import vm_hw
import vm_shots
from tests.vm.fake_env import Lab, gui_rules, guest_rules
from tests.vm.helpers import load_vm_module

vm = load_vm_module()
ROOT = Path(__file__).resolve().parents[2]


def ppm(width: int, height: int, color: bytes = b"\x10\x20\x30") -> bytes:
    return b"P6\n# a comment\n%d %d\n255\n" % (width, height) + color * (width * height)


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    assert data.startswith(vm_shots.PNG_SIGNATURE)
    position, pixels, size = 8, b"", (0, 0)
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        payload = data[position + 8:position + 8 + length]
        assert struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0] == zlib.crc32(kind + payload) & 0xFFFFFFFF
        if kind == b"IHDR":
            size = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            pixels += payload
        position += 12 + length
    return size[0], size[1], zlib.decompress(pixels)


# --- the pure parts ---------------------------------------------------------------------------------------------------------

def test_sizes_are_parsed_deduplicated_and_validated():
    assert vm_shots.parse_sizes("1366x768, 1920x1080,1366x768") == [(1366, 768), (1920, 1080)]
    assert vm_shots.size_name((1366, 768)) == "1366x768"
    for bad in ("", "1366", "1366x", "0x768", "12x12", "1366x768x2", "wide"):
        with pytest.raises(vm_shots.ShotsError):
            vm_shots.parse_sizes(bad)


def test_a_ppm_becomes_a_valid_png_with_the_same_pixels():
    png = vm_shots.ppm_to_png(ppm(3, 2, b"\xff\x00\x80"))
    width, height, raw = decode_png(png)
    assert (width, height) == (3, 2) == vm_shots.png_size(png)
    assert raw == (b"\x00" + b"\xff\x00\x80" * 3) * 2, "one filter byte per row, then the pixels"


def test_rows_padded_to_four_bytes_are_read_without_the_padding():
    """QEMU's screendump at 1366 pixels: 4098 bytes of colour per row, 4100 in the file. 3 pixels: 9 bytes, padded to 12."""
    rows = [bytes([10 + row]) * 9 for row in range(4)]
    padded = b"P6\n3 4\n255\n" + b"".join(row + b"\xee\xee\xee" for row in rows)
    width, height, raw = decode_png(vm_shots.ppm_to_png(padded))
    assert (width, height) == (3, 4)
    assert raw == b"".join(b"\x00" + row for row in rows), "no padding byte becomes a pixel, so no row shifts"
    # a plain PPM with a trailing newline is not mistaken for a padded one
    plain = b"P6\n3 4\n255\n" + b"".join(rows) + b"\n"
    assert decode_png(vm_shots.ppm_to_png(plain))[2] == b"".join(b"\x00" + row for row in rows)


@pytest.mark.parametrize("data", [b"", b"P5\n1 1\n255\n\x00", b"P6\n1 1\n65535\n\x00\x00", b"P6\n2 2\n255\n\x00\x00\x00", b"P6\n0 5\n255\n", b"P6\nx y\n255\n"])
def test_a_screendump_that_is_not_a_complete_ppm_is_refused(data):
    with pytest.raises(vm_shots.ShotsError):
        vm_shots.ppm_to_png(data)


def archive(members: dict[str, bytes], *, extra=()) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as handle:
        directory = tarfile.TarInfo(".")
        directory.type = tarfile.DIRTYPE
        handle.addfile(directory)
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
        for info in extra:
            handle.addfile(info)
    return base64.b64encode(buffer.getvalue()).decode()


PNG = vm_shots.ppm_to_png(ppm(4, 4))


def test_the_pictures_are_unpacked_into_the_directory(tmp_path):
    names = vm_shots.unpack(archive({"./page-00-welcome-guided.png": PNG, "./shots.json": b"{}"}), tmp_path / "out")
    assert names == ["page-00-welcome-guided.png", "shots.json"]
    assert (tmp_path / "out/page-00-welcome-guided.png").read_bytes() == PNG


def test_an_unsafe_or_broken_archive_writes_nothing_outside(tmp_path):
    link = tarfile.TarInfo("evil.png")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    for payload, fragment in (
        (archive({"../escape.png": PNG}), "not a picture"),
        (archive({"/abs.png": PNG}), "not a picture"),
        (archive({"sub/dir.png": PNG}), "not a picture"),
        (archive({"run.sh": b"#!/bin/sh"}), "not a picture"),
        (archive({"fake.png": b"not a png at all"}), "not a PNG"),
        (archive({}, extra=[link]), "not a picture"),
        (base64.b64encode(b"not a tar archive").decode(), "tar archive"),
        ("%%%not base64%%%", "base64"),
    ):
        with pytest.raises(vm_shots.ShotsError, match=fragment):
            vm_shots.unpack(payload, tmp_path / "out")
    assert not (tmp_path / "escape.png").exists() and not list(tmp_path.glob("**/escape.png"))


def index_of(size=(1366, 768), modes=("guided", "advanced"), pages=("welcome", "readiness", "storage", "system"), problems=()) -> dict:
    shown = [{"file": f"page-{number:02d}-{name}-{mode}.png", "index": number, "page": name, "mode": mode} for mode in modes for number, name in enumerate(pages)]
    return {"type": "gui_shots", "screen": list(size), "pages": shown, "problems": list(problems)}


def test_a_complete_set_at_the_right_size_has_no_problems():
    index = index_of()
    files = [page["file"] for page in index["pages"]]
    assert vm_shots.judge((1366, 768), index, (1366, 768), files) == []


@pytest.mark.parametrize(("changes", "fragment"), [
    ({"desktop": (1024, 768)}, "screendump of the desktop is 1024x768"),
    ({"index": index_of(size=(1280, 800))}, "screen is 1280x800"),
    ({"index": index_of(modes=("guided",))}, "no installer page was photographed in advanced mode"),
    ({"index": index_of(pages=("welcome",))}, "only 1 different pages"),
    ({"index": {"screen": [1366, 768], "pages": [], "problems": []}}, "took no picture"),
    ({"index": index_of(problems=["page-02-storage-guided.png: needs horizontal scrolling (QScrollArea)"])}, "installer: page-02-storage-guided.png: needs horizontal scrolling"),
    ({"files": []}, "did not arrive"),
])
def test_each_problem_of_a_set_is_named(changes, fragment):
    index = changes.get("index", index_of())
    files = changes.get("files", [page["file"] for page in index["pages"]])
    problems = vm_shots.judge((1366, 768), index, changes.get("desktop", (1366, 768)), files)
    assert any(fragment in problem for problem in problems), problems


def test_full_page_pictures_are_not_counted_as_pages():
    index = index_of()
    index["pages"].append({"file": "page-03-system-guided-full.png", "page": "system", "mode": "guided", "full_page_of": "page-03-system-guided.png"})
    files = [page["file"] for page in index["pages"]]
    assert vm_shots.judge((1366, 768), index, (1366, 768), files) == []


def test_the_display_size_becomes_qemu_arguments_for_the_adapters_that_can_announce_it():
    assert vm_hw.display_arguments(vm_hw.VMHardware()) == ["-vga", "virtio"], "without a size the command line is what it always was"
    assert vm_hw.display_arguments(vm_hw.VMHardware(resolution="1366x768")) == ["-device", "virtio-vga,edid=on,xres=1366,yres=768"]
    assert vm_hw.display_arguments(vm_hw.VMHardware(vga="std", resolution="1920x1080")) == ["-device", "VGA,edid=on,xres=1920,yres=1080"]
    assert vm_hw.display_arguments(vm_hw.VMHardware(vga="qxl", resolution="1920x1080")) == ["-device", "qxl-vga,xres=1920,yres=1080"]
    assert vm_hw.VMHardware(resolution="1366x768").problems() == []
    assert any("Invalid display size" in problem for problem in vm_hw.VMHardware(resolution="big").problems())
    assert any("needs the virtio, std, qxl adapter" in problem for problem in vm_hw.VMHardware(vga="cirrus", resolution="1366x768").problems())


def test_the_command_runs_the_guest_program_as_the_live_user_in_its_display():
    command = vm_shots.shots_command({"DISPLAY": ":0", "XAUTHORITY": "/home/gentoo/.Xauthority"})
    assert command.startswith("runuser -u gentoo -- env DISPLAY=:0 XAUTHORITY=/home/gentoo/.Xauthority python3 /run/vm-gui-shots.py /tmp/gentoo-vm-shots")
    assert vm_shots.fetch_command() == "tar -C /tmp/gentoo-vm-shots -cf - . | base64 -w0"


# --- the guest program (offscreen) --------------------------------------------------------------------------------------------

def load_shots_program():
    spec = importlib.util.spec_from_file_location("vm_gui_shots", ROOT / "scripts" / "vm-gui-shots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_guest_program_helpers():
    program = load_shots_program()
    assert program.slug("Base system!") == "base-system" and program.slug("") == "page"
    assert program.fit_problems((1000, 700), (1366, 768)) == []
    assert program.fit_problems((1400, 800), (1366, 768)) == ["the window is 1400 px wide on a 1366 px wide screen", "the window is 800 px high on a 768 px high screen"]
    with pytest.raises(SystemExit):
        program.build_parser().parse_args([])


@pytest.mark.slow
def test_the_guest_program_photographs_every_page_in_both_modes(tmp_path):
    """The real installer window on Qt's offscreen platform: one picture per page and mode, a valid index, the whole page for tall pages."""
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, str(ROOT / "scripts/vm-gui-shots.py"), str(tmp_path), "--socket", str(tmp_path / "no.sock"), "--scan-wait", "1", "--settle", "50"],
                          capture_output=True, text=True, timeout=240, env=environment)
    assert done.returncode == 0, done.stderr[-2000:]
    index = json.loads((tmp_path / "shots.json").read_text())
    assert json.loads(done.stdout.strip().splitlines()[-1]) == index
    pictures = sorted(path.name for path in tmp_path.glob("*.png"))
    shown = [page for page in index["pages"] if "full_page_of" not in page]
    assert len({page["page"] for page in shown}) >= 6 and {page["mode"] for page in shown} == {"guided", "advanced"}
    assert all(page["file"] in pictures for page in index["pages"])
    for name in pictures:
        vm_shots.png_size((tmp_path / name).read_bytes())
    assert index["screen"] and index["window"]


# --- the scenario against the fake lab ------------------------------------------------------------------------------------------

def size_rules(size: tuple[int, int], *, screen=None, modes=("guided", "advanced"), first: bool) -> list[dict]:
    """The guest's answers for one boot: the picture program's index, then the archive of pictures (the first boot answers first)."""
    index = index_of(size, modes=modes)
    index["screen"] = list(screen or size)
    files = {f"./{page['file']}": PNG for page in index["pages"]}
    files["./shots.json"] = json.dumps(index).encode()
    times = {"times": 1} if first else {}
    return [{"user": "^root$", "match": r"python3 /run/vm-gui-shots\.py", **times, "stdout": json.dumps(index) + "\n"},
            {"user": "^root$", "match": r"^tar -C /tmp/gentoo-vm-shots", **times, "stdout": archive(files)}]


def screenshot_lab_rules(*, sizes=((1366, 768), (1920, 1080)), **kwargs) -> list[dict]:
    rules = [{"user": "^root$", "match": r"^cat > /run/vm-gui-shots\.py", "stdin_to": "vm-gui-shots.py"}]
    for number, size in enumerate(sizes):
        rules += size_rules(size, first=number < len(sizes) - 1, **kwargs)
    return rules + gui_rules() + guest_rules(vm, "screenshots")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


@pytest.mark.slow
def test_the_screenshots_scenario_boots_once_per_size_and_keeps_every_picture(lab, tmp_path):
    lab.rules = screenshot_lab_rules()
    destination = tmp_path / "pictures"
    assert lab.run("--scenario", "screenshots", "--screenshots", str(destination), "--screenshot-settle", "1") == 0, lab.result().get("error")
    result = lab.result()["screenshots"]
    assert [item["size"] for item in result["sizes"]] == ["1366x768", "1920x1080"] and all(item["problems"] == [] for item in result["sizes"])
    for size in ("1366x768", "1920x1080"):
        width, height = map(int, size.split("x"))
        assert vm_shots.png_size((destination / size / "live-desktop.png").read_bytes()) == (width, height), "the desktop picture has the size of the display"
        assert (destination / size / "page-00-welcome-guided.png").is_file() and (destination / size / "page-03-system-advanced.png").is_file()
    assert json.loads((destination / "index.json").read_text())["sizes"][1]["size"] == "1920x1080"
    argv = json.loads((lab.run_dir() / "fake-qemu-argv-live.json").read_text())
    assert any("xres=1920,yres=1080" in argument for argument in argv), "the last boot was the 1920x1080 one"
    assert (lab.run_dir() / "fake-guest" / "vm-gui-shots.py").read_bytes() == (ROOT / "scripts/vm-gui-shots.py").read_bytes()


@pytest.mark.slow
def test_a_display_that_does_not_have_the_size_asked_for_fails_the_scenario(lab, tmp_path):
    lab.rules = screenshot_lab_rules(sizes=((1366, 768),), screen=(1024, 768))
    assert lab.run("--scenario", "screenshots", "--screenshot-sizes", "1366x768", "--screenshots", str(tmp_path / "p"), "--screenshot-settle", "1") == 1
    assert "screen is 1024x768, not 1366x768" in lab.result()["error"]


@pytest.mark.slow
def test_a_missing_mode_fails_the_scenario(lab, tmp_path):
    lab.rules = screenshot_lab_rules(sizes=((1366, 768),), modes=("guided",))
    assert lab.run("--scenario", "screenshots", "--screenshot-sizes", "1366x768", "--screenshots", str(tmp_path / "p"), "--screenshot-settle", "1") == 1
    assert "advanced mode" in lab.result()["error"]


@pytest.mark.slow
def test_a_bad_size_list_is_a_harness_error(lab, tmp_path):
    lab.rules = guest_rules(vm, "screenshots")
    assert lab.run("--scenario", "screenshots", "--screenshot-sizes", "huge", "--screenshots", str(tmp_path / "p")) == 1
    assert "not a screen size" in lab.result()["error"]
