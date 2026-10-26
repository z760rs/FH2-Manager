"""Turn a drag-in FH2 mod (a Mount folder or zip, replacing whole game files) into a change-based mod folder.

Every file the mod ships is compared with the stock game:
  stripped/gamedbRC.slt                   -> row changes            (changes/gamedb.json)
  stripped/stringtables/<lang>.zip / .str  -> string entry changes   (changes/strings.json)
  audio/.../MasterBank.strings.bank        -> event path changes     (changes/fmod_strings.json)
  zipmanifest.xml                         -> listed zips            (changes/zipmanifest.json)
  Mount/hash.manifest                     -> index values of new files (changes/hash_index.json)
  any other stock zip                     -> entry changes          (entries/<zip>/<entry>, mod.json "entries")
  any other stock file                    -> whole-file replacement (files/<path>, with the stock file's SHA-256)
  a file the game does not have           -> new file               (files/<path>)
Files identical to stock are ignored. Paths are relative to the game's media folder; whether the mod put a file in
media, mediapc or mediaoverride (Extra Options) is kept as "placed".
"""
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile

from . import gamedb
from .stock import FolderStock, as_source
from .formats import fmodstrings, hashmanifest, strtable, zipmanifest

FORMAT = 1
MEDIA_ROOTS = ("media", "mediapc", "mediaoverride")
STRINGS_DIR = "stripped/stringtables/"
FMOD_STRINGS = "masterbank.strings.bank"
MEDIA_ONLY = ("zipmanifest.xml",)          # files the game reads from media even with Extra Options installed


def sha256(b):
    return hashlib.sha256(b).hexdigest()


Stock = FolderStock          # kept for callers of earlier versions


class ModSource:
    """the mod: [(placed root, path relative to it, original spelling, reader)] from a folder or a zip"""

    def __init__(self, path, roots=MEDIA_ROOTS):
        self.files, self.other, self.unpacked, self.launcher = [], [], [], []
        if os.path.isdir(path) and os.path.isdir(os.path.join(path, "Mount")):
            path = os.path.join(path, "Mount")       # a whole game folder: only Mount belongs to the game data
        if os.path.isdir(path):
            items = []
            for root, _, files in os.walk(path):
                for f in files:
                    p = os.path.join(root, f)
                    items.append((os.path.relpath(p, path).replace(os.sep, "/"),
                                  _Item(lambda p=p: open(p, "rb").read(), os.path.getsize(p), p)))
        else:
            items = _zip_items(zipfile.ZipFile(path))
        if not any(_is_game_path(n) for n, _ in items):
            # a download that wraps the mod: the game files are in a zip inside it (e.g. "Pack.zip" next to a readme
            # and an "Update/azimuth-windows.zip"); use every inner zip that holds a Mount / media folder
            inner = []
            for n, rd in items:
                if n.lower().endswith(".zip"):
                    try:
                        zi = zipfile.ZipFile(io.BytesIO(rd()))
                        sub = _zip_items(zi)
                    except (zipfile.BadZipFile, RuntimeError):
                        continue
                    if any(_is_game_path(m) for m, _ in sub):
                        inner += [(n + "/" + m, r) for m, r in sub]
                        self.unpacked.append(n)
            if inner:
                items = inner + [(n, r) for n, r in items if n not in self.unpacked]
        self.launcher = [n for n, _ in items if re.search(r"(?i)(^|/)(azimuth-windows|data/bin)(/|.zip$)", n)]
        for name, rd in items:
            parts = name.split("/")
            low = [s.lower() for s in parts]
            if "mount" in low:                       # everything up to Mount/ is packaging
                k = low.index("mount")
                parts, low = parts[k + 1:], low[k + 1:]
            if low and low[0] in MEDIA_ROOTS and len(parts) > 1:
                if low[0] in roots:
                    self.files.append((low[0], "/".join(low[1:]), "/".join(parts[1:]), rd))
            elif len(parts) == 1 and low[0] == "hash.manifest":
                if "media" in roots:
                    self.other.append(("hash.manifest", rd))
            elif "media" in roots:
                self.other.append(("/".join(parts), rd))
        # a path shipped in several roots: the copy the game uses comes first (Extra Options: mediaoverride over
        # mediapc over media; zipmanifest.xml is read before Extra Options hooks in, so media's copy is the live one)
        def rank(f):
            root, rel = f[0], f[1]
            order = ("media", "mediaoverride", "mediapc") if rel in MEDIA_ONLY else ("mediaoverride", "mediapc", "media")
            return order.index(root)
        self.files.sort(key=rank)


def _zip_items(z):
    """[(name, _Item)] of a zip's files (encrypted entries are skipped)"""
    return [(i.filename, _Item(lambda n=i.filename: z.read(n), i.file_size, None))
            for i in z.infolist() if not i.is_dir() and not i.flag_bits & 1]


def _is_game_path(name):
    """the name holds a game file: <...>/Mount/<media root>/..., or starts with a media root"""
    low = name.lower().split("/")
    if "mount" in low:
        low = low[low.index("mount") + 1:]
    return len(low) > 1 and low[0] in MEDIA_ROOTS


class _Item:
    """a mod file: read() on demand, its size, its path on disk (None inside a zip)"""

    def __init__(self, read, size, path):
        self.read, self.size, self.path = read, size, path

    def __call__(self):
        return self.read()


def _zip_entries(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return {i.filename: z.read(i.filename) for i in z.infolist() if not i.is_dir()}


def _zip_owner(stock, rel):
    """(stock zip, entry) when a folder on rel's path is named like a stock zip next to it, else None"""
    parts = rel.split("/")
    for k in range(len(parts) - 1, 0, -1):
        z = "/".join(parts[:k]) + ".zip"
        if stock.has(z):
            return z, "/".join(parts[k:])
    return None


def _strings_key(rel):
    """('EN', 'data_car.str') for stripped/stringtables/en.zip:Data_Car.str or stripped/stringtables/en/data_car.str"""
    rest = rel[len(STRINGS_DIR):]
    if "/" in rest:
        lang, table = rest.split("/", 1)
        return lang, table
    return rest[:-4] if rest.endswith(".zip") else rest, None


def import_mod(stock_path, mod_path, out, name=None, version="0", roots=MEDIA_ROOTS, library=None, log=print):
    """stock_path: an unmodified game folder or a stock source (stock.GameBaseline); roots: the Mount folders to
    take from the mod (("mediapc", "mediaoverride") imports what Extra Options mods put in a game folder).
    library: the other mod folders already imported; car / part ids this mod uses for something else than they do
    are given new ids (renumber.py). A failed import leaves nothing behind."""
    if os.path.exists(out) and os.listdir(out):
        raise SystemExit("output folder %s is not empty" % out)
    created = not os.path.exists(out)
    try:
        report = _import_mod(stock_path, mod_path, out, name, version, roots)
        others = [p for p in (library or []) if os.path.abspath(p) != os.path.abspath(out)
                  and os.path.isfile(os.path.join(p, "mod.json"))]
        if others:
            from . import renumber
            moved = renumber.resolve(out, as_source(stock_path), others, log)
            if moved["cars"] or moved["parts"]:
                report = json.load(open(os.path.join(out, "mod.json"), encoding="utf-8"))
        return report
    except BaseException:
        if created:
            shutil.rmtree(out, ignore_errors=True)
        raise


def _import_mod(stock_path, mod_path, out, name, version, roots):
    stock, mod = as_source(stock_path), ModSource(mod_path, roots)
    if not mod.files and not any(k == "hash.manifest" for k, _ in mod.other):
        raise SystemExit("no game files found in %s (nothing under Mount/media, mediapc or mediaoverride%s)"
                         % (os.path.basename(mod_path.rstrip("/\\")),
                            ", also not in the zips inside it" if mod.unpacked or os.path.isfile(mod_path) else ""))
    os.makedirs(os.path.join(out, "changes"), exist_ok=True)
    report = {"format": FORMAT, "name": name or os.path.splitext(os.path.basename(mod_path.rstrip("/\\")))[0],
              "version": version,
              "source": {"file": os.path.basename(mod_path.rstrip("/\\")),
                         "sha256": sha256(open(mod_path, "rb").read()) if os.path.isfile(mod_path) else None},
              "base": {"gamedb_sha256": sha256(stock.read("stripped/gamedbrc.slt"))},
              "files": [], "entries": [], "unchanged": 0, "ignored": [], "provides": {}, "consumed": [],
              "unpacked_from": mod.unpacked, "launcher_files": mod.launcher}
    strings, new_files, keep, zip_cache = {}, [], {}, {}
    placed = {}
    seen = set()
    for root, rel, spelled, rd in mod.files:
        if rel in seen:
            report["ignored"].append({"path": rel, "placed": root, "why": "overridden by the copy in another media root"})
            report["consumed"].append(root + "/" + spelled)
            continue
        seen.add(rel)
        placed[rel] = root
        if stock.has(rel) and _same(stock, rel, rd):
            report["unchanged"] += 1
            continue
        data = rd()
        report["consumed"].append(root + "/" + spelled)
        if rel == "stripped/gamedbrc.slt":
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "mod.slt")
                open(p, "wb").write(data)
                d = gamedb.diff(stock.path(rel), p)
            keep["gamedb"] = data
            json.dump(d, open(os.path.join(out, "changes", "gamedb.json"), "w"), indent=1)
            report["gamedb"] = {"placed": root, "tables": gamedb.summary(d)}
            report["provides"] = provides(d)
        elif rel == "zipmanifest.xml":
            base = zipmanifest.parse(stock.read(rel).decode("utf-8-sig"))
            new = zipmanifest.parse(data.decode("utf-8-sig"))
            meta = ("version", "priority")
            zm = {"add": [{k: v for k, v in a.items() if k in meta or k == "path"} for p, a in new.items() if p not in base],
                  "remove": [p for p in base if p not in new],
                  "change": [{"path": p, "set": {k: new[p][k] for k in meta if new[p].get(k) != base[p].get(k)}}
                             for p in new if p in base and any(new[p].get(k) != base[p].get(k) for k in meta)]}
            json.dump(zm, open(os.path.join(out, "changes", "zipmanifest.json"), "w"), indent=1)
            report["zipmanifest"] = {"placed": root, "add": len(zm["add"]), "remove": len(zm["remove"]),
                                     "change": len(zm["change"])}
        elif rel.endswith(FMOD_STRINGS) and stock.has(rel):
            b, m = fmodstrings.bank_paths(stock.read(rel)), fmodstrings.bank_paths(data)
            keep["fmod"] = (rel, data)
            fd = {"bank": rel, "add": {p: g for p, g in m.items() if p not in b},
                  "change": {p: {"base": b[p], "new": g} for p, g in m.items() if p in b and b[p] != g},
                  "remove": sorted(p for p in b if p not in m)}
            json.dump(fd, open(os.path.join(out, "changes", "fmod_strings.json"), "w"), indent=1)
            report["fmod_strings"] = {"placed": root, "add": len(fd["add"]), "change": len(fd["change"]),
                                      "remove": len(fd["remove"])}
        elif rel.startswith(STRINGS_DIR) and (rel.endswith(".zip") or rel.endswith(".str")):
            lang, table = _strings_key(rel)
            zrel = STRINGS_DIR + lang + ".zip"
            stock_entries = {k.lower(): v for k, v in _zip_entries(stock.read(zrel)).items()} if stock.has(zrel) else {}
            mod_entries = {table: data} if table else {k.lower(): v for k, v in _zip_entries(data).items()}
            for t, md in mod_entries.items():
                if stock_entries.get(t) == md:
                    continue
                if t.endswith(".str") and t in stock_entries and strtable.supported(stock_entries[t]):
                    sd = strtable.diff(strtable.parse(stock_entries[t]), strtable.parse(md))
                    keep.setdefault("strings", {})[(lang.upper(), t)] = (stock_entries[t], md)
                    if any(sd.values()):
                        strings.setdefault(lang.upper(), {})[t] = sd
                else:
                    _entry(out, report, zrel, t, md, stock_entries.get(t), root)
        elif stock.has(rel) and rel.endswith(".zip"):
            se, me = _zip_entries(stock.read(rel)), _zip_entries(data)
            sl = {k.lower(): k for k in se}
            for e, md in me.items():
                base = se.get(sl.get(e.lower()))
                if base != md:
                    _entry(out, report, rel, e, md, base, root)
            for e in se:
                if e.lower() not in {k.lower() for k in me}:
                    report["entries"].append({"zip": rel, "entry": e, "op": "remove", "base_sha256": sha256(se[e])})
        elif not stock.has(rel) and _zip_owner(stock, rel):
            # a loose file under a folder named like a stock zip (EntityModel/x.xml -> EntityModel.zip): the game path
            # is the same as that zip's entry, so it is an entry change of the zip (Extra Options serves such files as
            # overrides of the zip's entries)
            zrel, entry = _zip_owner(stock, rel)
            if zrel not in zip_cache:
                zip_cache[zrel] = {k.lower(): (k, v) for k, v in _zip_entries(stock.read(zrel)).items()}
            have = zip_cache[zrel].get(entry.lower())
            if have is None or have[1] != data:
                _entry(out, report, zrel, have[0] if have else spelled.split("/", spelled.count("/") - entry.count("/"))[-1],
                       data, have[1] if have else None, root)
        else:
            _file(out, report, rel, spelled, data, stock.read(rel) if stock.has(rel) else None, root)
            if not stock.has(rel):
                new_files.append(rel)
    if strings:
        json.dump(strings, open(os.path.join(out, "changes", "strings.json"), "w", encoding="utf-8"), indent=1,
                  ensure_ascii=False)
        report["strings"] = {lang: {t: "+%d ~%d -%d" % (len(d["add"]), len(d["change"]), len(d["remove"]))
                                    for t, d in tabs.items()} for lang, tabs in strings.items()}
    for kind, rd in mod.other:
        if kind == "hash.manifest":
            raw = rd()
            base, new = hashmanifest.parse(stock.read_mount("hash.manifest") or b""), hashmanifest.parse(raw)
            if new != base:
                report["consumed"].append("hash.manifest")
            added = {k: v for k, v in new.items() if k not in base}
            vals = {}
            for rel in new_files:
                k = hashmanifest.key("media\\" + rel)
                if k in added:
                    vals[rel] = added.pop(k)
            json.dump({"files": vals, "unmatched": {"%016x" % k: v for k, v in added.items()}},
                      open(os.path.join(out, "changes", "hash_index.json"), "w"), indent=1)
            report["hash_index"] = {"files": len(vals), "unmatched": len(added)}
        else:
            sp = stock.mount_path(kind)
            if sp and _same_file(sp, rd):
                continue                              # a stock game file next to media (exe, appx data, ...)
            report["ignored"].append({"path": kind, "why": "outside the media folders (not imported)"})
    report["new_files_without_index_value"] = [f for f in new_files
                                               if f not in _load(out, "hash_index.json").get("files", {})]
    report["warnings"] = warnings(out, report)
    report["verified"] = verify(stock, out, keep)
    json.dump(report, open(os.path.join(out, "mod.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return report


def verify(stock, out, keep):
    """re-apply the extracted changes to stock and compare with the mod's own files: nothing may be lost"""
    from . import apply
    done = []
    if "gamedb" in keep:
        with tempfile.TemporaryDirectory() as td:
            base, mine = os.path.join(td, "base.slt"), os.path.join(td, "mod.slt")
            shutil.copyfile(stock.path("stripped/gamedbrc.slt"), base)
            open(mine, "wb").write(keep["gamedb"])
            apply.gamedb_apply(base, _load(out, "gamedb.json"))
            again = gamedb.diff(mine, base)
            if not gamedb.is_empty(again):
                raise AssertionError("gamedb: re-applied changes differ from the mod: %s %s"
                                     % (sorted(again["tables"]), gamedb.structure(again)))
        done.append("gamedb")
    if "fmod" in keep:
        rel, data = keep["fmod"]
        rebuilt = apply.fmod_apply(stock.read(rel), _load(out, "fmod_strings.json"))
        assert fmodstrings.bank_paths(rebuilt) == fmodstrings.bank_paths(data), "fmod strings: re-applied paths differ"
        done.append("fmod_strings")
    if "strings" in keep:
        tabs = _load_text(out, "strings.json")
        for (lang, t), (base, mine) in keep["strings"].items():
            rebuilt = apply.strings_apply(base, tabs[lang][t])
            key = lambda b: sorted((r["tag"], r["text"], r["aux"], r["name"]) for r in strtable.parse(b)["rows"])
            assert key(rebuilt) == key(mine), "strings %s %s: re-applied rows differ" % (lang, t)
        done.append("strings (%d tables)" % len(keep["strings"]))
    return done


def _load_text(out, name):
    p = os.path.join(out, "changes", name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def warnings(out, report):
    """changes to STOCK content (as opposed to additions): what can break other cars or other mods"""
    w = []
    fm = _load(out, "fmod_strings.json")
    if fm.get("remove"):
        w.append("removes %d stock sound path(s), e.g. %s - the stock cars using them may lose their sound"
                 % (len(fm["remove"]), ", ".join(fm["remove"][:3])))
    if fm.get("change"):
        w.append("changes the GUID of %d stock sound path(s)" % len(fm["change"]))
    gd = _load(out, "gamedb.json")
    if gd:
        st = gamedb.structure(gd)
        if st:
            w.append("changes the database structure (%s) - other mods that write to these tables may conflict"
                     % "; ".join(st))
    for t, ch in gd.get("tables", {}).items():
        if ch.get("update"):
            w.append("edits %d stock row(s) of %s" % (len(ch["update"]), t))
        if ch.get("delete") or ch.get("remove"):
            w.append("deletes %d stock row(s) of %s" % (len(ch.get("delete", [])) + len(ch.get("remove", [])), t))
    for lang, tabs in _load_text(out, "strings.json").items():
        for t, d in tabs.items():
            if d["change"] or d["remove"]:
                w.append("%s %s: changes %d / removes %d stock string(s)" % (lang, t, len(d["change"]), len(d["remove"])))
    for f in report["files"]:
        if f["op"] == "replace":
            w.append("replaces the stock file %s as a whole" % f["path"])
    for e in report["entries"]:
        if e["op"] in ("replace", "remove"):
            w.append("%s stock entry %s in %s" % ("replaces" if e["op"] == "replace" else "removes", e["entry"], e["zip"]))
    return w


def _same_file(stock_path, item):
    if item.path:
        try:
            if os.path.samefile(item.path, stock_path):
                return True
        except OSError:
            pass
    return item.size == os.path.getsize(stock_path) and item() == open(stock_path, "rb").read()


def _same(stock, rel, item):
    """the mod file equals the stock one (a hard link / the same file, else same size and bytes)"""
    sp = stock.path(rel)
    if item.path:
        try:
            if os.path.samefile(item.path, sp):
                return True
        except OSError:
            pass
    if item.size != os.path.getsize(sp):
        return False
    return item() == stock.read(rel)


def _load(out, name):
    p = os.path.join(out, "changes", name)
    return json.load(open(p)) if os.path.exists(p) else {}


def _file(out, report, rel, spelled, data, base, root):
    dst = os.path.join(out, "files", *spelled.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "wb").write(data)
    report["files"].append({"path": rel, "file": "files/" + spelled, "op": "replace" if base is not None else "add",
                            "sha256": sha256(data), "base_sha256": sha256(base) if base is not None else None,
                            "size": len(data), "placed": root})


def _entry(out, report, zrel, entry, data, base, root):
    dst = os.path.join(out, "entries", *zrel.split("/"), *entry.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "wb").write(data)
    report["entries"].append({"zip": zrel, "entry": entry, "op": "replace" if base is not None else "add",
                              "file": "entries/%s/%s" % (zrel, entry), "sha256": sha256(data),
                              "base_sha256": sha256(base) if base is not None else None, "placed": root})


def provides(d):
    """cars / makes a database diff adds (for conflict checks between mods)"""
    out = {}
    t = d["tables"]
    if "Data_Car" in t:
        cols = t["Data_Car"]["columns"]
        out["cars"] = [{"id": r[cols.index("Id")], "media_name": r[cols.index("MediaName")]}
                       for r in t["Data_Car"].get("insert", [])]
    if "List_CarMake" in t:
        cols = t["List_CarMake"]["columns"]
        out["makes"] = [r[cols.index("ID")] for r in t["List_CarMake"].get("insert", [])]
    out["inserted_keys"] = {name: len(ch.get("insert", [])) for name, ch in t.items() if ch.get("insert")}
    return out
