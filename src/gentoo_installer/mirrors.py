"""PORT: where programs are downloaded from (Handbook: Optional, Selecting mirrors).

Three ways to choose: Automatic (distfiles.gentoo.org, which sends each visitor to a server near them), Choose by country
(an ordered list from the bundled table below) and Custom URL. Every address is HTTPS: the installer never writes a plain
http:// mirror, because the files would arrive unprotected. The stage3 itself is always checked against Gentoo's signature,
whichever server it comes from.

The table was checked on 2026-10-25 (each server answered for releases/amd64/autobuilds/latest-stage3-amd64-openrc.txt).
"""

from __future__ import annotations

import re
from typing import NamedTuple

AUTOMATIC = "https://distfiles.gentoo.org"
SOURCE_AUTOMATIC, SOURCE_COUNTRY, SOURCE_CUSTOM = "automatic", "country", "custom"
SOURCES = (SOURCE_AUTOMATIC, SOURCE_COUNTRY, SOURCE_CUSTOM)

_URL = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%+-]+)*/?")


class Mirror(NamedTuple):
    country: str  # ISO 3166 code
    name: str
    url: str


COUNTRIES = {
    "AU": "Australia", "BE": "Belgium", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CN": "China", "CZ": "Czechia",
    "DE": "Germany", "FR": "France", "GB": "United Kingdom", "JP": "Japan", "KR": "South Korea", "NL": "Netherlands",
    "RU": "Russia", "SE": "Sweden", "US": "United States",
}

MIRRORS: tuple[Mirror, ...] = (
    Mirror("AU", "AARNet", "https://mirror.aarnet.edu.au/pub/gentoo"),
    Mirror("BE", "BELNET", "https://ftp.belnet.be/pub/rsync.gentoo.org/gentoo"),
    Mirror("BR", "Federal University of Parana (C3SL)", "https://gentoo.c3sl.ufpr.br"),
    Mirror("CA", "University of Waterloo", "https://mirror.csclub.uwaterloo.ca/gentoo-distfiles"),
    Mirror("CH", "Init7", "https://mirror.init7.net/gentoo"),
    Mirror("CN", "Tsinghua University", "https://mirrors.tuna.tsinghua.edu.cn/gentoo"),
    Mirror("CN", "USTC", "https://mirrors.ustc.edu.cn/gentoo"),
    Mirror("CZ", "DKM", "https://mirror.dkm.cz/gentoo"),
    Mirror("DE", "FAU Erlangen", "https://ftp.fau.de/gentoo"),
    Mirror("FR", "IRCAM", "https://mirrors.ircam.fr/pub/gentoo-distfiles"),
    Mirror("GB", "UK Mirror Service", "https://www.mirrorservice.org/sites/distfiles.gentoo.org"),
    Mirror("JP", "JAIST", "https://ftp.jaist.ac.jp/pub/Linux/Gentoo"),
    Mirror("JP", "RIKEN", "https://ftp.riken.jp/Linux/gentoo"),
    Mirror("KR", "Kakao", "https://mirror.kakao.com/gentoo"),
    Mirror("NL", "Leaseweb", "https://mirror.leaseweb.com/gentoo"),
    Mirror("NL", "University of Twente", "https://ftp.snt.utwente.nl/pub/os/linux/gentoo"),
    Mirror("RU", "Yandex", "https://mirror.yandex.ru/gentoo-distfiles"),
    Mirror("SE", "Lysator", "https://ftp.lysator.liu.se/gentoo"),
    Mirror("US", "MIT", "https://mirrors.mit.edu/gentoo-distfiles"),
    Mirror("US", "Oregon State University", "https://gentoo.osuosl.org"),
    Mirror("US", "Rochester Institute of Technology", "https://mirrors.rit.edu/gentoo"),
)


def mirrors_for_country(code: str) -> list[Mirror]:
    return [mirror for mirror in MIRRORS if mirror.country == code]


def countries() -> list[tuple[str, str]]:
    """(code, name) of every country that has a mirror, by name."""
    return sorted(((code, COUNTRIES[code]) for code in {mirror.country for mirror in MIRRORS}), key=lambda item: item[1])


def url_problem(url: str) -> str:
    """Why this is not a usable mirror address ("" when it is one): HTTPS only, no spaces, no query."""
    text = url.strip()
    if text.startswith("http://"):
        return "Plain http:// mirrors are not offered: the downloads would not be protected. Use an https:// address."
    if not text.startswith("https://") or not _URL.fullmatch(text):
        return "A mirror address looks like https://mirror.example.org/gentoo"
    return ""


def mirror_list(primary: str, mirrors: list[str] | tuple[str, ...] = (), fallbacks: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Every server downloads may use, in order: the chosen mirrors (else ``primary``), then the fallbacks.

    Duplicates (also ones that differ only by a trailing slash) are dropped. This is the list GENTOO_MIRRORS gets, and the
    stage3 download tries the first entry and then the others.
    """
    ordered: list[str] = []
    for url in [*(mirrors or [primary]), *fallbacks]:
        url = url.strip().rstrip("/")
        if url and url not in ordered:
            ordered.append(url)
    return ordered
