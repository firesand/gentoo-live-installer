#!/usr/bin/env python3
"""Regenerate atoms.tsv and eqglob.tsv from the host Portage (read-only; nothing is executed but its parsers).

    python3 tests/fixtures/gen_fixtures.py          # the committed, sampled fixtures
    python3 tests/fixtures/gen_fixtures.py --full   # every corpus row (about 5000 atoms), for a thorough one-off check

atoms.tsv    "valid<TAB>atom": is the string a valid `portage.dep.Atom(s, allow_wildcard=True, allow_repo=True)`?
eqglob.tsv   "matches<TAB>=cat/pkg-PREFIX*<TAB>candidate version": does `portage.dep.match_from_list` say a
             `=cat/pkg-PREFIX*` atom matches the installed `cat/pkg-<candidate>`?

The Rust tests (portage::atom) check their parser against these rows, so a Portage change shows up as a diff here.
"""
import os
import sys
import zlib

from portage.dep import Atom, InvalidAtom, match_from_list
from portage.exception import InvalidData

HERE = os.path.dirname(os.path.abspath(__file__))


def atom_corpus():
    ops = ["", "=", ">=", "<=", ">", "<", "~", "!", "!!", "==", "=>", "*"]
    names = [
        "app-misc/foo", "app-misc/fo*", "app-*/foo", "*/foo", "app-misc/*", "*/*", "dev-qt/qt*", "*-misc/foo",
        "app-m*c/f*o", "app-misc/foo-bar", "app-misc/foo.bar", "app-misc/foo+", "app-misc/foo_bar", "dev-cpp/gtk++",
        "app.misc/foo", "app_misc/foo", "app+misc/foo", "-app/foo", "app-misc/-foo", "app-misc/foo-", "app-misc/2foo",
        "app-misc/foo2", "virtual/pkg", "acct-user/foo", "app-misc/f**", "**/foo", "app-misc", "foo", "/foo", "app-misc/",
        "", "app-misc/foo bar", "app-misc/foo/bar", "APP-misc/Foo", "app-misc/foo~",
    ]
    versions = ["", "-1.0", "-1.2.3", "-1.0-r1", "-1.0_rc1", "-1.0_p20240101", "-1.0a", "-1", "-1.", "-.1", "-1.0*", "-1*",
                "-9999", "-1.0_foo", "-1.0-r", "-1.0-r1-r2", "-0", "-00.1"]
    tails = ["", ":1", ":1/2", ":=", ":*", ":1=", ":", ":/", ":1/", ":/2", ":1.2", ":foo", ":*1", "::gentoo", "::", "::g-1",
             "::-x", ":1::gentoo", "[a]", "[a,b]", "[]", "[a(+)]", "[-a]", "[a?]", "[!a?]", "[a=]", "[!a=]", "[a,]", "[ a ]",
             "[a(+)(-)]", "[-a?]", "[!a]", "[a(*)]", "::gentoo[a]", ":=[a]"]
    seen = set()
    out = []

    def add(s):
        if s not in seen:
            seen.add(s)
            out.append(s)

    for name in names:
        for op in ops:
            for ver in versions[:9]:
                add(op + name + ver)
    for name in names[:12]:
        for ver in versions:
            for op in ("", "=", ">=", "~"):
                add(op + name + ver)
        for tail in tails:
            add(name + tail)
            add("=" + name + "-1.0" + tail)
            add(">=" + name + "-1.0-r1" + tail)
    for extra in ["=dev-lang/python-*3.1*", "=app-misc/foo-*1*", "!app-misc/foo", "!!app-misc/foo", "!!!app-misc/foo",
                  "dev-libs/libsodium:=", "app-misc/foo-1.0-1", "=app-misc/foo-1.0-1", "=app-misc/foo-1.0-01"]:
        add(extra)
    return out


def valid_atom(s):
    try:
        Atom(s, allow_wildcard=True, allow_repo=True)
    except (InvalidAtom, InvalidData, ValueError, TypeError):
        return False
    return True


def unsupported(s):
    """Valid for Portage, but not something the config editor models: binary-package build ids
    (=cat/pkg-1.0-1) and extended version globs (=cat/pkg-*1*). Those atoms are left out."""
    try:
        atom = Atom(s, allow_wildcard=True, allow_repo=True)
    except Exception:
        return False
    return atom.build_id is not None or (atom.operator == "=*" and (atom.version or "").startswith("*"))


def eqglob_rows():
    prefixes = ["1", "1.2", "1.1", "1.0", "6.1", "6", "0.5", "2", "1.2.3", "10", "1_rc", "1.0_p", "3.11", "20", "01", "0", "1a"]
    candidates = ["1", "1.0", "1.00", "1.1", "1.10", "1.12", "1.2", "1.2.3", "1.2.30", "1.20", "1.3", "1.5", "10.0", "2", "6.1", "6.1.190",
                  "6.12.113", "6.18.57", "6.2", "0.5", "0.50", "0.5.1", "01", "01.2", "1-r1", "1.2-r1", "1.2_rc1", "1.2_p1", "1_rc1", "1.0_p20240101",
                  "1.0a", "1a", "1b", "3.11.5", "3.1.2", "3.110", "20240101", "2024", "9999", "1.0.0", "1.00.1", "1.0_alpha", "1.0_rc2"]
    rows = []
    for p in prefixes:
        atom = Atom("=app-misc/foo-%s*" % p)
        for c in candidates:
            try:
                got = bool(match_from_list(atom, ["app-misc/foo-%s" % c]))
            except (KeyError, InvalidData):
                continue
            rows.append((got, "=app-misc/foo-%s*" % p, c))
    return rows


MUST_KEEP = [
    "dev-qt/qt*", "app-*/foo", "app-misc/fo*", "*/*", "app-misc/*", "*/foo", "app-misc/foo", "=app-misc/foo-1.2*", "~app-misc/foo-1.2",
    ">=app-misc/foo-1.0-r1:1/2=::gentoo", "!app-misc/foo", "!!app-misc/foo", "dev-libs/libsodium:=", "dev-libs/libsodium:*",
    "app-misc/foo-1.0", "app-misc/foo:", "app-misc/foo::", "app-misc/foo[]", "app-misc/foo.bar", "dev-cpp/gtk++-3.0", "dev-cpp/gtk++",
    "=dev-cpp/gtk++-3.0", "app-misc/foo-bar-1", "notanatom", "", "*", "=app-misc/foo", ">=app-misc/foo", "app.misc/foo",
]


def sample(atoms, full):
    """Every seventh row by a stable hash, plus the cases the code was written for."""
    if full:
        return atoms
    return [s for s in atoms if s in MUST_KEEP or zlib.crc32(s.encode()) % 7 == 0]


def main():
    full = "--full" in sys.argv[1:]
    atoms = sample([s for s in atom_corpus() if not unsupported(s)], full)
    with open(os.path.join(HERE, "atoms.tsv"), "w") as f:
        f.write("# valid<TAB>atom: portage.dep.Atom(s, allow_wildcard=True, allow_repo=True); generated by gen_fixtures.py\n")
        for s in atoms:
            assert "\t" not in s and "\n" not in s
            f.write(f"{int(valid_atom(s))}\t{s}\n")
    rows = eqglob_rows()
    with open(os.path.join(HERE, "eqglob.tsv"), "w") as f:
        f.write("# matches<TAB>atom<TAB>candidate version: portage.dep.match_from_list; generated by gen_fixtures.py\n")
        for got, atom, cand in rows:
            f.write(f"{int(got)}\t{atom}\t{cand}\n")
    print(f"{len(atoms)} atoms ({sum(valid_atom(s) for s in atoms)} valid), {len(rows)} =glob rows", file=sys.stderr)


if __name__ == "__main__":
    main()
