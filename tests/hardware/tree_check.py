"""Resolve plan atoms against a Portage tree. Run by test_tree_evidence.py in its own process.

    python tree_check.py TREE SCRATCH < cases.json > result.json

Each case describes a Portage configuration (profile, make.conf, package.use,
package.license, package.mask) and atoms. The result says, per atom, which
version is visible and whether its USE flags exist and its REQUIRED_USE holds.
Everything is written below SCRATCH; TREE is only read.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

tree, scratch = Path(sys.argv[1]), Path(sys.argv[2])
for sub in ("tmp", "depcache", "distfiles", "pkgdir", "root"):
    (scratch / sub).mkdir(parents=True, exist_ok=True)
os.environ.update({"PORTAGE_TMPDIR": str(scratch / "tmp"), "PORTAGE_DEPCACHEDIR": str(scratch / "depcache"),
                   "DISTDIR": str(scratch / "distfiles"), "PKGDIR": str(scratch / "pkgdir")})
sys.dont_write_bytecode = True

import portage  # noqa: E402
from portage.dep import check_required_use, use_reduce  # noqa: E402


def configure(index: int, case: dict) -> dict:
    cfg = scratch / f"cfg{index}"
    if cfg.exists():
        shutil.rmtree(cfg)
    (cfg / "etc/portage/repos.conf").mkdir(parents=True)
    (cfg / "etc/portage/repos.conf/gentoo.conf").write_text(f"[DEFAULT]\nmain-repo = gentoo\n\n[gentoo]\nlocation = {tree}\nauto-sync = no\n")
    os.symlink(tree / "profiles" / case["profile"], cfg / "etc/portage/make.profile")
    (cfg / "etc/portage/make.conf").write_text(case["make_conf"])
    (cfg / "etc/portage/package.use").write_text("".join(f"{atom} {' '.join(flags)}\n" for atom, flags in case["package_use"].items()))
    (cfg / "etc/portage/package.license").write_text("".join(f"{atom} {' '.join(items)}\n" for atom, items in case["package_license"].items()))
    (cfg / "etc/portage/package.mask").write_text("".join(line + "\n" for line in case["package_mask"]))
    (cfg / "etc/portage/package.accept_keywords").write_text("".join(f"{atom} {' '.join(words)}\n" for atom, words in case.get("package_keywords", {}).items()))
    trees = portage.create_trees(config_root=str(cfg), target_root=str(scratch / "root"))
    return trees[next(iter(trees))]


def check(trees: dict, atom: str, flags: list[str]) -> dict:
    pdb = trees["porttree"].dbapi
    best = pdb.xmatch("bestmatch-visible", atom)
    if not best:
        newest = pdb.xmatch("match-all", atom)
        # getmaskingstatus() locks the settings it is given, so give it a throw-away clone.
        reasons = portage.getmaskingstatus(newest[-1], settings=portage.config(clone=pdb.settings), portdb=pdb) if newest else ["no such package"]
        return {"visible": None, "why": reasons}
    keys = ["KEYWORDS", "LICENSE", "IUSE", "REQUIRED_USE"]
    keywords, licence, iuse_text, required = pdb.aux_get(best, keys)
    iuse = {item.lstrip("+-") for item in iuse_text.split()}
    config = portage.config(clone=trees["vartree"].settings)
    config.setcpv(best, mydb=pdb)
    enabled = set(config.get("PORTAGE_USE", "").split())
    satisfied = True
    if required.strip():
        satisfied = bool(check_required_use(required, enabled, lambda flag: flag in iuse))
    return {"visible": best.split("/", 1)[1], "keywords": keywords, "licence": sorted(set(use_reduce(licence, matchall=True, flat=True)) - {"||"}),
            "unknown_flags": sorted(flag for flag in flags if flag.lstrip("-") not in iuse), "required_use_ok": satisfied,
            "enabled": sorted(flag for flag in enabled if flag in iuse)}


cases = json.load(sys.stdin)
output = []
for index, case in enumerate(cases):
    trees = configure(index, case)
    result = {"id": case["id"], "atoms": {}}
    for atom in case["atoms"]:
        result["atoms"][atom] = check(trees, atom, case["package_use"].get(atom, []))
    output.append(result)
json.dump(output, sys.stdout)
