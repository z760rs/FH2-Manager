"""FH2 Manager command line.

  python -m fh2manager import --stock <unmodified game> --mod <mod folder or zip> --out <new mod folder>
         [--name NAME] [--version VERSION]
  python -m fh2manager build --stock <unmodified game> --mod <mod> [--mod <mod> ...] --out <new folder>
         [--new-files media|mediapc] [--index-donor <new file>=<stock file> ...]
A --mod for build is a converted mod folder or anything import accepts. Later mods have priority.
Full results go to <out>/mod.json or <out>/build.json; the console gets a summary.
"""
import argparse
import json
import os

from . import builder, importer


def summary(r, out):
    lines = ["imported %s -> %s" % (r["source"]["file"], out)]
    if r.get("unpacked_from"):
        lines.append("  unpacked: the game files are in %s" % ", ".join(r["unpacked_from"]))
    cars = r.get("provides", {}).get("cars", [])
    if cars:
        lines.append("  cars:     " + ", ".join("%s (%d)" % (c["media_name"], c["id"]) for c in cars))
    if r.get("provides", {}).get("makes"):
        lines.append("  makes:    " + ", ".join(map(str, r["provides"]["makes"])))
    if "gamedb" in r:
        t = r["gamedb"]["tables"]
        lines.append("  gamedb:   %d tables changed (from %s)" % (len(t), r["gamedb"]["placed"]))
    if "strings" in r:
        tabs = sorted({t for lang in r["strings"].values() for t in lang})
        lines.append("  strings:  %s in %d languages" % (", ".join(tabs), len(r["strings"])))
    if "fmod_strings" in r:
        f = r["fmod_strings"]
        lines.append("  sounds:   +%d paths, %d changed, %d removed" % (f["add"], f["change"], f["remove"]))
    if "zipmanifest" in r:
        lines.append("  zips:     +%d listed" % r["zipmanifest"]["add"])
    for f in r["files"]:
        lines.append("  file:     %s %s (%.1f MB)" % (f["op"], f["path"], f["size"] / 1e6))
    if r["entries"]:
        lines.append("  entries:  %d zip entries" % len(r["entries"]))
    if r.get("new_files_without_index_value"):
        lines.append("  no hash.manifest value shipped for: " + ", ".join(r["new_files_without_index_value"]))
    for old, new in r.get("renumbered", {}).get("cars", {}).items():
        lines.append("  NEW ID:   car %s -> %s (another library mod uses %s)" % (old, new, old))
    for w in r.get("warnings", []):
        lines.append("  WARNING:  " + w)
    if r.get("launcher_files"):
        lines.append("  NOTE:     the mod also ships %d Azimuth-folder file(s) (plugins / launcher), e.g. %s - not "
                     "installed by FH2 Manager; copy them into the Azimuth folder by hand if you want them"
                     % (len(r["launcher_files"]), r["launcher_files"][0].split("/")[-1]))
    if r["ignored"]:
        lines.append("  ignored:  %d file(s), e.g. %s" % (len(r["ignored"]), r["ignored"][0]["path"]))
    lines.append("  unchanged stock files skipped: %d" % r["unchanged"])
    if r.get("verified"):
        lines.append("  verified: stock + changes reproduce the mod's " + ", ".join(r["verified"]))
    return "\n".join(lines)


def library_mods(lib):
    return [os.path.join(lib, d) for d in sorted(os.listdir(lib)) if os.path.isfile(os.path.join(lib, d, "mod.json"))
            and not d.endswith((".renumbering", ".before_renumber"))]


def build_summary(r, out):
    lines = ["built %s from %s" % (out, " + ".join("%s %s" % (m["name"], m["version"]) for m in r["mods"]))]
    for o in r["outputs"]:
        lines.append("  %-8s %s (%.1f MB) <- %s" % ("replace" if o["replaces_stock_sha256"] else "new", o["path"],
                                                   o["size"] / 1e6, ", ".join(o["from"])))
    hm = r["hash_manifest"]
    if hm["new_records"]:
        lines.append("  hash.manifest: +%d records (%s)" % (len(hm["new_records"]),
                     "; ".join("%s %s" % (x["path"], x["value_source"]) for x in hm["new_records"])))
        lines.append("  Azimuth must be patched to accept the new hash.manifest")
    for n in r["notes"]:
        lines.append("  note: " + n)
    lines.append("  verified: zips open and match zipmanifest.xml, database integrity ok, tables parse")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(prog="fh2manager", description="FH2 Manager: Forza Horizon 2 (PC) mod manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    im = sub.add_parser("import", help="convert a drag-in mod into a change-based mod folder")
    im.add_argument("--stock", required=True, help="unmodified game: its Mount folder or the folder containing Mount")
    im.add_argument("--mod", required=True, help="the mod: a folder or zip (Mount/media, Mount/mediapc, ...)")
    im.add_argument("--out", required=True, help="new, empty output folder")
    im.add_argument("--name")
    im.add_argument("--version", default="0")
    im.add_argument("--library", help="the mod library: ids this mod uses for something else than a library mod "
                                      "are given new ones")
    rn = sub.add_parser("renumber", help="give a library mod new ids where it clashes with the other library mods "
                                         "(only for a mod whose cars are not in your save yet)")
    rn.add_argument("--stock", required=True)
    rn.add_argument("--mod", required=True, help="the converted mod folder to renumber")
    rn.add_argument("--library", required=True)
    bd = sub.add_parser("build", help="combine stock + mods into one install tree")
    bd.add_argument("--stock", required=True)
    bd.add_argument("--mod", action="append", required=True, help="repeat; lowest priority first")
    bd.add_argument("--out", required=True)
    bd.add_argument("--new-files", choices=("media", "mediapc"), default="media")
    bd.add_argument("--index-donor", action="append", default=[], metavar="NEW=STOCK",
                    help="hash.manifest value for a new file, taken from this stock file (paths relative to media)")
    for name, hlp in (("status", "what is installed in a game folder and what blocks an install"),
                      ("adopt", "import the changes already in a game folder as one mod (before installing)"),
                      ("install", "install mods into a game folder (replaces what FH2 Manager installed before)"),
                      ("uninstall", "restore the game folder and the launcher"),
                      ("repatch", "re-patch the Azimuth folder for what is installed (new Azimuth build / folder)")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--game", required=True, help="the game folder Azimuth launches (contains Mount)")
        sp.add_argument("--azimuth", help="the folder with azimuth.exe (patched when mods add files)")
        sp.add_argument("--stock", help="optional unmodified copy, for game files changed by hand before")
        if name == "adopt":
            sp.add_argument("--library", required=True)
            sp.add_argument("--name", default="existing-mods")
        if name == "install":
            sp.add_argument("--mod", action="append", default=[], help="converted mod folder; repeat, lowest priority "
                                                                      "first; none = back to stock")
        if name in ("install", "uninstall"):
            sp.add_argument("--take-over", action="store_true", help="also replace files of mods FH2 Manager did not "
                                                                     "install (they are backed up)")
            sp.add_argument("--allow-owned-removal", action="store_true", help="remove cars even when the save "
                                                                               "looks like it owns them")
    fx = sub.add_parser("fh2patch", help="install / remove FH2Patch (launcher plugin loader) and its plugins")
    fx.add_argument("--game", required=True)
    fx.add_argument("--azimuth", required=True)
    fx.add_argument("--package", help="the FH2Patch release zip (or its unpacked folder)")
    fx.add_argument("--plugin", action="append", default=[], help="plugin to enable, e.g. FH2_NODIRT; repeat")
    fx.add_argument("--remove", action="store_true")
    fp = sub.add_parser("fingerprint", help="write the stock fingerprint list from an unmodified game (maintainers)")
    fp.add_argument("--stock", required=True)
    fp.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "import":
        print(summary(importer.import_mod(a.stock, a.mod, a.out, a.name, a.version,
                                          library=library_mods(a.library) if a.library else None), a.out))
    elif a.cmd == "renumber":
        from . import renumber
        from .stock import FolderStock
        try:
            moved = renumber.resolve(a.mod, FolderStock(a.stock), [p for p in library_mods(a.library)
                                                                    if os.path.abspath(p) != os.path.abspath(a.mod)])
        except renumber.RenumberError as e:
            raise SystemExit("renumber failed: %s" % e)
        print("nothing clashes" if not (moved["cars"] or moved["parts"]) else "renumbered: %s" % moved)
    elif a.cmd == "build":
        donors = dict(x.lower().split("=", 1) for x in a.index_donor)
        try:
            r = builder.build(a.stock, a.mod, a.out, a.new_files, donors)
        except builder.BuildError as e:
            raise SystemExit("build failed: %s" % e)
        print(build_summary(r, a.out))
    elif a.cmd == "fingerprint":
        from . import fingerprint
        d = fingerprint.make(a.stock, a.out)
        print("%d files -> %s" % (len(d["files"]), a.out))
    elif a.cmd == "fh2patch":
        from .installer import Game, InstallError
        try:
            g = Game(a.game, a.azimuth)
            if a.remove:
                g.remove_fh2patch()
            elif not a.package:
                raise SystemExit("--package is needed to install")
            else:
                g.install_fh2patch(a.package, a.plugin)
        except InstallError as e:
            raise SystemExit("fh2patch failed: %s" % e)
    else:
        from .installer import Game, InstallError
        try:
            g = Game(a.game, a.azimuth, a.stock)
            if a.cmd == "status":
                st = g.status()
                st["launcher_check"] = g.launcher_check()
                print(json.dumps(st, indent=1))
            elif a.cmd == "adopt":
                print(summary(g.adopt(a.library, a.name), a.name))
            elif a.cmd == "install":
                g.apply(a.mod, take_over=a.take_over, allow_owned_removal=a.allow_owned_removal)
            elif a.cmd == "uninstall":
                g.uninstall(take_over=a.take_over, allow_owned_removal=a.allow_owned_removal)
            elif a.cmd == "repatch":
                if not a.azimuth:
                    raise SystemExit("--azimuth is needed")
                g.repatch_launcher()
        except InstallError as e:
            raise SystemExit("%s failed: %s" % (a.cmd, e))


if __name__ == "__main__":
    main()
