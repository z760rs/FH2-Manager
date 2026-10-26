"""Combine stock + any number of change-based mods (in priority order) into one install tree.

Output (<out>/):
  Mount/...        the files to install, laid out like the game's Mount folder
  build.json       every output file (SHA-256, the stock file it replaces, the mods it comes from), the mods,
                   the hash.manifest records added, and whether Azimuth must accept a new hash.manifest

Where things go (Extra Options):
  mediapc/stripped/gamedbRC.slt               the combined database
  mediapc/stripped/stringtables/<lang>/*.str  only the string tables a mod changed (loose; Extra Options overrides
                                              single entries of the stringtable zips this way)
  media/zipmanifest.xml                       the game reads it before Extra Options takes effect
  media/audio/fmodanthem/durango/MasterBank.strings.bank
  media/<path>                                new files (car zips, sound banks, ...) and stock files a mod replaces or
                                              patches; new files get hash.manifest records (the game cannot open a file
                                              that has none) -> Mount/hash.manifest, and Azimuth must be patched to
                                              accept it (the installer does that)
  --new-files mediapc puts new files in mediapc instead, without hash.manifest records (experimental).

Conflicts stop the build with a report: the same database key inserted by two mods with different contents (an
identical row is added once - converted cars share engines / parts), a row or string or sound path that is not what
a mod expects, the same file / zip entry with different contents, the same car in two mods, two different cars with
one id, duplicate sound GUIDs.
"""
import collections
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile

from . import apply, gamedb, importer
from .stock import as_source
from .formats import fmodstrings, forzazip, hashmanifest, strtable, zipmanifest

DB = "stripped/gamedbrc.slt"
# tables whose integer key no other table refers to (checked on the stock database: no column of another table holds
# these values), so a colliding insert can take the next free key. FM5-importer mods each add their paint rows at
# "highest stock Combo_Colors.ID + 1".
RENUMBER = {"Combo_Colors": "ID"}
FMOD = "audio/fmodanthem/durango/masterbank.strings.bank"


class BuildError(Exception):
    pass


def sha256(b):
    return hashlib.sha256(b).hexdigest()


class Mod:
    def __init__(self, path):
        self.path = path
        self.meta = json.load(open(os.path.join(path, "mod.json"), encoding="utf-8"))
        if self.meta.get("format") != importer.FORMAT:
            raise BuildError("%s: unsupported mod format %r" % (path, self.meta.get("format")))
        self.name = self.meta["name"]

    def changes(self, name):
        p = os.path.join(self.path, "changes", name)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    def read(self, rel_file):
        return open(os.path.join(self.path, *rel_file.split("/")), "rb").read()


def load_mods(paths, stock_path, work):
    """change-based mod folders as given; anything else (a downloaded zip / Mount folder) is imported first"""
    mods = []
    for k, p in enumerate(paths):
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "mod.json")):
            mods.append(Mod(p))
        else:
            out = os.path.join(work, "import%d" % k)
            importer.import_mod(stock_path, p, out)
            mods.append(Mod(out))
    names = [m.name for m in mods]
    dup = [n for n, c in collections.Counter(names).items() if c > 1]
    if dup:
        raise BuildError("the same mod is given twice: %s" % ", ".join(dup))
    return mods


def check_provides(mods):
    """two mods adding the same car (same name: keep one ticked), or two different cars with one id"""
    problems = []
    by_id, by_name = {}, {}
    twice = collections.OrderedDict()               # (first mod, mod) -> cars both add
    for m in mods:
        for c in m.meta.get("provides", {}).get("cars", []):
            name = str(c["media_name"])
            first = by_name.get(name.lower()) or by_id.get(c["id"])
            if first:
                fm, fc = first
                if str(fc["media_name"]).lower() == name.lower():
                    twice.setdefault((fm, m.name), []).append(name)
                else:
                    problems.append("car id %s is used by two different cars: %s in %s and %s in %s - only one of "
                                    "them can be installed" % (c["id"], fc["media_name"], fm, name, m.name))
                continue
            by_id[c["id"]] = by_name[name.lower()] = (m.name, c)
    for (fm, mn), cars in twice.items():
        problems.append("%s and %s both add the same car%s %s%s - keep only one of them ticked"
                        % (fm, mn, "s" if len(cars) > 1 else "", ", ".join(cars[:4]),
                           " and %d more" % (len(cars) - 4) if len(cars) > 4 else ""))
    return problems


def build(stock_path, mod_paths, out, new_files="media", index_donors=None):
    stock = as_source(stock_path)
    if os.path.exists(out) and os.listdir(out):
        raise BuildError("output folder %s is not empty" % out)
    work = tempfile.mkdtemp(prefix="fh2manager-")
    try:
        mods = load_mods(mod_paths, stock, work)
        return _build(stock, mods, out, new_files, index_donors or {})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stock_name(stock, rel):
    """the stock file's own spelling, relative to media"""
    return stock.spelled(rel)


def _build(stock, mods, out, new_files, index_donors):
    problems = check_provides(mods)
    if problems:
        raise BuildError("conflicts:\n  " + "\n  ".join(problems))
    mount = os.path.join(out, "Mount")
    outputs = {}                    # path relative to Mount -> {"data", "from", "replaces"}
    notes = ["%s: %s" % (m.name, w) for m in mods for w in m.meta.get("warnings", [])]
    rewritten = []                  # zips put into the native layout, reported once at the end

    def emit(rel_mount, data, frm, replaces=None):
        outputs[rel_mount] = {"data": data, "from": sorted(set(frm)), "replaces": replaces}

    # --- whole files and zip entries
    files, entries = {}, collections.defaultdict(dict)
    for m in mods:
        for f in m.meta["files"]:
            if f["op"] == "replace":
                if not stock.has(f["path"]) or sha256(stock.read(f["path"])) != f["base_sha256"]:
                    raise BuildError("%s replaces %s, but this game's copy is not the one it was made for"
                                     % (m.name, f["path"]))
            data = m.read(f["file"])
            if f["path"] in files and sha256(files[f["path"]][0]) != sha256(data):
                prev, frm, spelled = files[f["path"]]
                if f["op"] == "add" and f["path"].endswith(".zip"):
                    # the same NEW zip from several mods (e.g. a thumbnail / logo pack every FM5-importer car mod
                    # calls stripped/RC23.zip): merge the entries; only the same entry with other bytes conflicts
                    data = _merge_zips(prev, data, f["path"], frm, m.name)
                    notes.append("%s: entries of %s and %s merged" % (f["path"], frm, m.name))
                    files[f["path"]] = (data, "%s+%s" % (frm, m.name), spelled)
                    continue
                raise BuildError("%s and %s both ship %s with different contents" % (frm, m.name, f["path"]))
            files[f["path"]] = (data, m.name, f["file"][len("files/"):])
        for e in m.meta["entries"]:
            key = (e["zip"], e["entry"].lower())
            data = None if e["op"] == "remove" else m.read(e["file"])
            if key in entries[e["zip"]] and entries[e["zip"]][key][0] != data:
                raise BuildError("%s and %s both change %s in %s" % (entries[e["zip"]][key][1], m.name, e["entry"], e["zip"]))
            entries[e["zip"]][key] = (data, m.name, e["entry"])
    for rel, (data, frm, spelled_mod) in files.items():
        if rel.endswith(".zip"):
            iss = forzazip.issues(data)
            if iss:
                data = forzazip.native(data)
                rewritten.append((rel, frm, iss))
        new = not stock.has(rel)
        root = new_files if new else "media"
        spelled = spelled_mod if new else _stock_name(stock, rel)
        emit(root + "/" + spelled, data, [frm], None if new else sha256(stock.read(rel)))
    for zrel, ch in entries.items():
        if zrel.startswith("stripped/stringtables/"):
            continue
        if zrel in files:
            raise BuildError("%s is both replaced whole and patched entry by entry" % zrel)
        if not stock.has(zrel):
            raise BuildError("entries for %s, which the game does not have" % zrel)
        base = stock.read(zrel)
        emit("media/" + _stock_name(stock, zrel), forzazip.patched(base, {n: d for d, _, n in ch.values()}),
             [f for _, f, _ in ch.values()], sha256(base))

    # --- database
    dbs = [(m, m.changes("gamedb.json")) for m in mods if m.changes("gamedb.json")]
    if dbs:
        fd, tmp = tempfile.mkstemp(suffix=".slt")
        os.close(fd)
        try:
            shutil.copyfile(stock.path(DB), tmp)
            shared = {}
            for m, d in dbs:
                try:
                    renamed = []
                    apply.gamedb_apply(tmp, d, RENUMBER, renamed, shared, m.name)
                    notes.extend("%s: %s" % (m.name, n) for n in renamed)
                except apply.Conflict as e:
                    raise BuildError("database conflict applying %s: %s" % (m.name, e))
            c = sqlite3.connect(tmp)
            ok = c.execute("pragma integrity_check").fetchone()[0]
            c.close()
            if ok != "ok":
                raise BuildError("combined database failed its integrity check: %s" % ok)
            emit("mediapc/" + _stock_name(stock, DB), open(tmp, "rb").read(), [m.name for m, _ in dbs],
                 sha256(stock.read(DB)))
        finally:
            os.remove(tmp)

    # --- string tables
    tables = collections.defaultdict(list)            # (lang zip rel, table lower) -> [(mod, change set)]
    for m in mods:
        for lang, tabs in (m.changes("strings.json") or {}).items():
            zrel = next((r for r in stock.names if r.startswith("stripped/stringtables/") and
                         r[len("stripped/stringtables/"):-4].upper() == lang.upper() and r.endswith(".zip")), None)
            if zrel is None:
                raise BuildError("%s changes strings of language %s, which the game does not have" % (m.name, lang))
            for t, d in tabs.items():
                tables[(zrel, t)].append((m, d))
    for m in mods:                                    # whole-entry string tables (formats strtable can't rebuild)
        for e in m.meta["entries"]:
            if e["zip"].startswith("stripped/stringtables/"):
                tables[(e["zip"], e["entry"].lower())].append((m, {"whole": m.read(e["file"])} if e["op"] != "remove" else None))
    for (zrel, t), sets in sorted(tables.items()):
        z = zipfile.ZipFile(stock.path(zrel))
        names = {n.lower(): n for n in z.namelist()}
        if t not in names:
            whole = [s for _, s in sets if s and "whole" in s]
            if len(sets) != 1 or not whole:
                raise BuildError("string table %s is new in several mods: %s" % (t, [m.name for m, _ in sets]))
            data, entry = whole[0]["whole"], t
        else:
            data, entry = z.read(names[t]), names[t]
            for m, d in sets:
                if d is None or "whole" in d:
                    if len(sets) > 1:
                        raise BuildError("%s replaces %s:%s whole and other mods change it too" % (m.name, zrel, t))
                    if d is not None:
                        data = d["whole"]
                    continue
                try:
                    data = apply.strings_apply(data, d)
                except apply.Conflict as e:
                    raise BuildError("string conflict in %s %s applying %s: %s" % (zrel, t, m.name, e))
        lang_dir = os.path.splitext(os.path.basename(_stock_name(stock, zrel)))[0]
        emit("mediapc/stripped/stringtables/%s/%s" % (lang_dir, entry), data, [m.name for m, _ in sets],
             sha256(z.read(names[t])) if t in names else None)

    # --- FMOD strings bank
    fm = [(m, m.changes("fmod_strings.json")) for m in mods if m.changes("fmod_strings.json")]
    if fm:
        bank = stock.read(FMOD)
        for m, d in fm:
            try:
                bank = apply.fmod_apply(bank, d)
            except apply.Conflict as e:
                raise BuildError("sound conflict applying %s: %s" % (m.name, e))
        paths = fmodstrings.bank_paths(bank)
        dup = [g for g, c in collections.Counter(paths.values()).items() if c > 1]
        if dup:
            raise BuildError("sound GUIDs used by more than one path (two mods cloned the same sound without new "
                             "GUIDs?): %s" % [p for p, g in paths.items() if g in dup][:6])
        emit("media/" + _stock_name(stock, FMOD), bank, [m.name for m, _ in fm], sha256(stock.read(FMOD)))

    # --- zipmanifest.xml
    zm_rel = "zipmanifest.xml"
    text = stock.read(zm_rel).decode("utf-8-sig")
    listed = zipmanifest.parse(text)
    want = {}                                        # game path -> attrs
    for m in mods:
        for a in (m.changes("zipmanifest.json") or {}).get("add", []):
            want[a["path"].lower()] = a
    built_zips = {}
    for rel_mount, o in outputs.items():
        root, _, rel = rel_mount.partition("/")
        if rel.lower().endswith(".zip") and not rel.lower().startswith("stripped/stringtables/"):
            built_zips[zipmanifest.game_path(rel)] = o["data"]
    for gp in built_zips:
        if gp not in listed and gp not in want:
            if gp.startswith("game:\\media\\cars\\") and gp.count("\\") == 3:
                want[gp] = {"path": gp, "version": "1", "priority": "80"}
                notes.append("%s was not listed in zipmanifest.xml by its mod; added" % gp)
    lines = text.split("\n")
    changed = False
    for i, line in enumerate(lines):
        m_ = zipmanifest.LINE.search(line)
        if m_:
            gp = dict(zipmanifest.ATTR.findall(m_.group(1)))["path"].lower()
            if gp in built_zips:
                lines[i] = _zip_line(line, built_zips[gp])
                changed = True
    for gp, a in want.items():
        if gp in listed:
            continue
        if gp not in built_zips:
            raise BuildError("a mod lists %s in zipmanifest.xml but ships no such zip" % gp)
        end = next(i for i in range(len(lines) - 1, -1, -1) if "</ZipFiles>" in lines[i])
        eol = "\r" if lines[0].endswith("\r") else ""
        new = '  <Zip version="%s" path="%s" priority="%s" dirstart="0" dirsize="0" direntries="0" />%s' % (
            a.get("version", "1"), a["path"], a.get("priority", "80"), eol)
        lines.insert(end, _zip_line(new, built_zips[gp]))
        changed = True
    if changed:
        emit("media/" + _stock_name(stock, zm_rel), ("﻿" + "\n".join(lines)).encode("utf-8"),
             [m.name for m in mods if m.changes("zipmanifest.json")] or ["(builder)"], sha256(stock.read(zm_rel)))

    # --- hash.manifest
    hm = {"new_records": [], "azimuth_patch_needed": False}
    new_native = [rel_mount[len("media/"):] for rel_mount, o in outputs.items()
                  if rel_mount.startswith("media/") and o["replaces"] is None]
    if new_native:
        base_bytes = stock.read_mount("hash.manifest")
        records = list(hashmanifest.parse(base_bytes).items())
        have = dict(records)
        vals = {}
        for m in mods:
            vals.update((m.changes("hash_index.json") or {}).get("files", {}))
        for rel in sorted(new_native):
            k = hashmanifest.key("media\\" + rel)
            if k in have:
                continue
            v, how = vals.get(rel.lower()), "from the mod"
            if v is None:
                donor = index_donors.get(rel.lower()) or _donor(stock, rel)
                if donor is None:
                    raise BuildError("no hash.manifest value for %s: give --index-donor %s=<stock file>" % (rel, rel))
                v, how = have[hashmanifest.key("media\\" + donor.replace("/", "\\"))], "as stock " + donor
            records.append((k, v))
            have[k] = v
            hm["new_records"].append({"path": "media/" + rel, "value": v, "value_source": how})
        emit("hash.manifest", hashmanifest.build(records), ["(builder)"], sha256(base_bytes))
        hm["azimuth_patch_needed"] = True

    # --- write + record
    if rewritten:
        by_mod = collections.Counter(frm for _, frm, _ in rewritten)
        notes.append("%d zip(s) rewritten in FH2's native layout (%s) - from %s"
                     % (len(rewritten), "; ".join(sorted({i for _, _, iss in rewritten for i in iss})),
                        ", ".join("%s: %d" % kv for kv in sorted(by_mod.items()))))
    notes = condense(notes)
    report = {"tool": "fh2manager", "format": 1, "mods": [{"name": m.name, "version": m.meta.get("version"),
                                                          "source": m.meta.get("source")} for m in mods],
              "new_files": new_files, "outputs": [], "hash_manifest": hm, "notes": notes}
    for rel_mount, o in sorted(outputs.items()):
        dst = os.path.join(mount, *rel_mount.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "wb").write(o["data"])
        report["outputs"].append({"path": rel_mount, "sha256": sha256(o["data"]), "size": len(o["data"]),
                                  "replaces_stock_sha256": o["replaces"], "from": o["from"]})
    json.dump(report, open(os.path.join(out, "build.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    verify(out, report)
    return report


def _merge_zips(a, b, rel, name_a, name_b):
    """entries of zip b added to zip a (native layout); the same entry with different bytes is a conflict"""
    za, zb = zipfile.ZipFile(io.BytesIO(a)), zipfile.ZipFile(io.BytesIO(b))
    have = {n.lower(): n for n in za.namelist()}
    add = {}
    for n in zb.namelist():
        if n.lower() in have:
            if za.read(have[n.lower()]) != zb.read(n):
                raise BuildError("%s and %s both ship %s, and both have a different %s in it" % (name_a, name_b, rel, n))
        else:
            add[n] = zb.read(n)
    return forzazip.patched(a, add) if add else a


def condense(notes):
    """the same note for every language ("mod: XX table.str: ...") becomes one line naming the languages"""
    out, groups = [], collections.OrderedDict()
    for n in notes:
        m = re.match(r"^(.*?: )?([A-Z]{2,3}) (\S+\.str): (.*)$", n)
        if m:
            key = (m.group(1) or "", m.group(3), m.group(4))
            if key not in groups:
                groups[key] = []
                out.append(key)
            groups[key].append(m.group(2))
        else:
            out.append(n)
    return [x if isinstance(x, str) else "%s%s: %s (in %d language(s): %s)" % (x[0], x[1], x[2], len(groups[x]),
                                                                              " ".join(groups[x]))
            for x in out]


def _zip_line(line, data):
    s, z, n = zipmanifest.central_dir(data)
    for k, v in (("dirstart", s), ("dirsize", z), ("direntries", n)):
        line = re.sub(r'%s="\d+"' % k, '%s="%d"' % (k, v), line, count=1)
    return line


def _donor(stock, rel):
    """a stock file of the same kind whose hash.manifest value a new file reuses: same folder and extension"""
    folder, name = os.path.split(rel)
    ext = os.path.splitext(name)[1].lower()
    cands = sorted(r for r in stock.names if os.path.split(r)[0] == folder.lower() and r.endswith(ext))
    pref = [r for r in cands if os.path.basename(r)[:3] == name.lower()[:3]]     # same prefix (PG_, rc, ...)
    pick = (pref or cands)
    return _stock_name(stock, pick[0]) if pick else None


def verify(out, report):
    """re-read the written tree: zips open and match their manifest lines, the database opens, tables parse"""
    mount = os.path.join(out, "Mount")
    zm = next((o for o in report["outputs"] if o["path"].lower() == "media/zipmanifest.xml"), None)
    lines = zipmanifest.parse(open(os.path.join(mount, *zm["path"].split("/")), encoding="utf-8-sig").read()) if zm else {}
    for o in report["outputs"]:
        p = os.path.join(mount, *o["path"].split("/"))
        data = open(p, "rb").read()
        assert sha256(data) == o["sha256"], o["path"]
        low = o["path"].lower()
        if low.endswith(".zip") and "/stringtables/" not in low:
            assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None, o["path"]
            assert not forzazip.issues(data), (o["path"], forzazip.issues(data))
            gp = zipmanifest.game_path(o["path"].split("/", 1)[1])
            if gp in lines:
                s, z, n = zipmanifest.central_dir(data)
                a = lines[gp]
                assert (int(a["dirstart"]), int(a["dirsize"]), int(a["direntries"])) == (s, z, n), gp
        elif low.endswith(".str"):
            strtable.parse(data)
        elif low.endswith(".slt"):
            c = gamedb.open_ro(p)
            assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
            c.close()
        elif low.endswith("masterbank.strings.bank"):
            fmodstrings.bank_paths(data)
