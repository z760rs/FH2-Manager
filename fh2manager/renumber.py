"""Give an imported mod its own ids where they clash with other mods in the library.

Car converters keep the source game's ids (an FM5 car keeps its FM5 id, its engines their FM5 engine ids), and they
only check them against stock FH2. Two mods can therefore use one id for different cars or parts. The newly imported
mod is renumbered, once, when it enters the library: saves store car and part ids, so an id never changes after that.

A car (Data_Car.Id = O) moves to a free id N:
  - columns that hold the car id itself (Ordinal, CarId, ModelId, Data_Car.Id, OnDiscContent.ContentId): O -> N
  - car-derived ids O*1000 + k (car bodies, upgrade rows, physics, colours, and whatever the mod derives that way):
    -> N*1000 + k, in the rows the car owns, and elsewhere where the mod itself defines that id
  - the car zip's .carbin: the model id at +19 and the car-derived part ids in it
  - the car's thumbnail, when the mod ships it under a name with the id
A part whose id another mod uses for different contents moves to a free id, with every reference to it. Parts whose
references are not known (wheels, makes, ...) are left as they are and reported.
"""
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import zipfile

from . import apply, gamedb, importer
from .formats import forzazip

DB = "stripped/gamedbrc.slt"
SKIP = {"Combo_Colors"}             # renumbered at build time (nothing refers to its ID)
# part tables whose key may move: (table, key column) -> does a column (lower case) refer to that key
PART_REFS = {
    ("Data_Engine", "EngineID"): lambda c: c == "engineid",
    ("Data_Drivetrain", "DrivetrainID"): lambda c: c == "drivetrainid",
    ("Data_Motor", "MotorID"): lambda c: c == "motorid",
    ("List_TorqueCurve", "TorqueCurveID"): lambda c: c.startswith("torquecurve") and c.endswith("id"),
    ("List_AeroPhysics", "AeroPhysicsID"): lambda c: c == "aerophysicsid",
    ("List_SpringDamperPhysics", "SpringDamperPhysicsID"): lambda c: c.endswith("springdamperphysicsid"),
    ("List_AntiSwayPhysics", "AntiSwayPhysicsID"): lambda c: c == "antiswayphysicsid",
    ("List_TireCompound", "TireCompoundID"): lambda c: c == "tirecompoundid",
}


class RenumberError(Exception):
    pass


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _car_col(t, c):
    """a column holding a car id itself"""
    lc = c.lower()
    return lc in ("ordinal", "modelid") or lc.endswith("carid") or (t == "Data_Car" and lc == "id") or \
        (t == "OnDiscContent" and lc == "contentid")


def _id_col(c):
    lc = c.lower()
    return lc.endswith("id") or lc == "ordinal"


def _load(mod_dir):
    meta = json.load(open(os.path.join(mod_dir, "mod.json"), encoding="utf-8"))
    p = os.path.join(mod_dir, "changes", "gamedb.json")
    g = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else None
    return meta, g


def _inserted(g):
    """{(table, key column): {key value: row}} of a change set's single-column inserts"""
    out = {}
    for t, ch in (g or {}).get("tables", {}).items():
        if len(ch["key"]) == 1:
            i = ch["columns"].index(ch["key"][0])
            out[(t, ch["key"][0])] = {r[i]: r for r in ch.get("insert", [])}
    return out


def _stock_keys(stock_db, t, k, lo=None, hi=None):
    c = sqlite3.connect("file:%s?mode=ro" % stock_db.replace("\\", "/"), uri=True)
    try:
        if lo is None:
            return c.execute('select max("%s") from "%s"' % (k, t)).fetchone()[0] or 0
        return c.execute('select count(*) from "%s" where "%s" between ? and ?' % (k, t), (lo, hi)).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        c.close()


def plan(mod_dir, stock_db, others):
    """what has to move: {"cars": [(old id, media name, clashing mod, clashing car)], "parts": [(table, key column,
    value, clashing mod)], "unresolved": [message]}. others: the other mod folders of the library."""
    meta, g = _load(mod_dir)
    mine = _inserted(g)
    taken_cars, taken = {}, {}
    for o in others:
        om, og = _load(o)
        for c in om.get("provides", {}).get("cars", []):
            taken_cars.setdefault(c["id"], (om["name"], c["media_name"]))
        for tk, rows in _inserted(og).items():
            for v, r in rows.items():
                taken.setdefault(tk, {}).setdefault(v, (om["name"], r))
    out = {"cars": [], "parts": [], "unresolved": []}
    moving = set()
    for c in meta.get("provides", {}).get("cars", []):
        if c["id"] in taken_cars and str(taken_cars[c["id"]][1]).lower() != str(c["media_name"]).lower():
            out["cars"].append((c["id"], c["media_name"]) + taken_cars[c["id"]])
            moving.add(c["id"])
    for (t, k), rows in sorted(mine.items()):
        if t in SKIP or (t, k) not in taken:
            continue
        for v, r in rows.items():
            if v not in taken[(t, k)] or taken[(t, k)][v][1] == r:
                continue
            if _int(v) and any(v == m or m * 1000 <= v <= m * 1000 + 999 for m in moving):
                continue                            # moves with its car
            if (t, k) in PART_REFS or (t.startswith("List_Upgrade") and k.lower() == "id"):
                out["parts"].append((t, k, v, taken[(t, k)][v][0]))
            else:
                out["unresolved"].append("%s %s %s is used by %s for something else, and FH2 Manager cannot move "
                                         "this kind of id" % (t, k, v, taken[(t, k)][v][0]))
    return out


def _free(start, used_ids, used_keys, stock_db, stock_tables):
    """the first id from start that is unused, with its derived range id*1000 .. +999 unused too"""
    n = start
    while True:
        lo, hi = n * 1000, n * 1000 + 999
        if n not in used_ids and n not in used_keys and not any(lo <= v <= hi for v in used_keys) and                 not any(_stock_keys(stock_db, t, k, lo, hi) for t, k in stock_tables):
            return n
        n += 1


def _part_col(t, c):
    """a part key or a reference to one (where ids derived from an engine / drivetrain live: torque curves, ...)"""
    lc = c.lower()
    return (t.startswith("List_Upgrade") and lc == "id") or         any((t == pt and c == pk) or ref(lc) for (pt, pk), ref in PART_REFS.items())


def _move(tables, owner, old, new, defined, remapped, row_hooks=None, where=None):
    """old -> new in the columns owner(table, column) says hold that id; derived ids old*1000 + k -> new*1000 + k in
    the rows it owns, and where the mod itself defines that id (in the columns where(table, column) allows)"""
    lo, hi = old * 1000, old * 1000 + 999
    for t, ch in tables.items():
        cols = ch["columns"]
        own_cols = [i for i, c in enumerate(cols) if owner(t, c)]
        id_cols = [i for i, c in enumerate(cols) if _id_col(c)]
        for r in ch.get("insert", []):
            own = any(r[i] == old for i in own_cols)
            for i in own_cols:
                if r[i] == old:
                    r[i] = new
            for i in id_cols:
                v = r[i]
                if _int(v) and lo <= v <= hi and (own or (v in defined and (where is None or where(t, cols[i])))):
                    r[i] = new * 1000 + v - lo
                    remapped[v] = r[i]
            if own and row_hooks and t in row_hooks:
                row_hooks[t](r, cols)
        for u in ch.get("update", []):
            for c, v in u["set"].items():
                if owner(t, c) and v == old:
                    u["set"][c] = new
                elif _id_col(c) and _int(v) and lo <= v <= hi and v in defined and (where is None or where(t, c)):
                    u["set"][c] = new * 1000 + v - lo


def resolve(mod_dir, stock, others, log=print):
    """renumber the mod at mod_dir (in place) so it no longer clashes with the mods in others; returns what moved
    {"cars": {old: new}, "parts": {"table.column": {old: new}}} (empty when nothing clashed)"""
    stock_db = stock.path(DB)
    p = plan(mod_dir, stock_db, others)
    done = {"cars": {}, "parts": {}}
    if p["unresolved"]:
        raise RenumberError("this mod clashes with the library in a way that cannot be fixed by renumbering:\n  " +
                            "\n  ".join(p["unresolved"]))
    if not p["cars"] and not p["parts"]:
        return done
    work = mod_dir.rstrip("/\\") + ".renumbering"     # all or nothing: the mod is swapped in once checked
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(mod_dir, work)
    try:
        done = _renumber(work, stock_db, others, p, log)
        check(work, stock, others)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    old = mod_dir.rstrip("/\\") + ".before_renumber"
    os.rename(mod_dir, old)
    os.rename(work, mod_dir)
    shutil.rmtree(old)
    return done


def _renumber(mod_dir, stock_db, others, p, log):
    done = {"cars": {}, "parts": {}}
    meta, g = _load(mod_dir)
    tables = g["tables"]
    # every id in use: stock, the other mods, this mod
    used_ids, used_keys = set(), set()
    for o in others:
        om, og = _load(o)
        used_ids |= {c["id"] for c in om.get("provides", {}).get("cars", [])}
        for rows in _inserted(og).values():
            used_keys |= {v for v in rows if _int(v)}
    used_ids |= {c["id"] for c in meta.get("provides", {}).get("cars", [])}
    for rows in _inserted(g).values():
        used_keys |= {v for v in rows if _int(v)}
    stock_tables = [(t, ch["key"][0]) for t, ch in tables.items() if len(ch["key"]) == 1]
    defined = {v for rows in _inserted(g).values() for v in rows if _int(v)}

    # --- cars
    remapped = {}                                   # old car-derived id -> new, for the carbin
    start = max([_stock_keys(stock_db, "Data_Car", "Id")] + list(used_ids)) + 1
    for old, name, other, other_car in p["cars"]:
        new = _free(start, used_ids, used_keys, stock_db, stock_tables)
        start = new + 1
        used_ids.add(new)
        used_keys |= set(range(new * 1000, new * 1000 + 1000))

        def thumb(r, cols, old=old, new=new):
            r[cols.index("Thumbnail")] = _thumbnail(mod_dir, meta, r[cols.index("Thumbnail")], old, new, log)
        _move(tables, _car_col, old, new, defined, remapped, {"Data_Car": thumb})
        _carbin(mod_dir, meta, name, old, new, remapped, log)
        done["cars"][old] = new
        log("car %s: id %s -> %s (%s uses %s for %s)" % (name, old, new, other, old, other_car))
        importer_warning(meta, "car %s was given id %s (its id %s is used by %s for %s)" % (name, new, old, other,
                                                                                          other_car))

    # --- parts: the part and its references; ids derived from it (engine 420 -> camshaft upgrades 420000 + k) too
    top = {}
    for t, k, v, other in p["parts"]:
        key = (t, k)
        if key not in top:
            mine = [r[tables[t]["columns"].index(k)] for r in tables[t].get("insert", [])]
            top[key] = max([_stock_keys(stock_db, t, k)] + _column_values(others, t, k) + [x for x in mine if _int(x)])
        new = top[key] = _free(top[key] + 1, set(), used_keys, stock_db, stock_tables)
        used_keys |= {new} | set(range(new * 1000, new * 1000 + 1000))
        refers = PART_REFS.get(key, lambda c: False)
        _move(tables, lambda tt, c, t=t, k=k, refers=refers: (tt == t and c == k) or refers(c.lower()),
              v, new, defined, {}, where=_part_col)
        done["parts"].setdefault("%s.%s" % (t, k), {})[v] = new
        log("%s %s %s -> %s (%s uses %s for different contents)" % (t, k, v, new, other, v))
    if done["parts"]:
        importer_warning(meta, "parts given new ids (their ids are used by other mods for something else): %s"
                         % "; ".join("%s %s" % (tk, ", ".join("%s->%s" % x for x in m.items()))
                                     for tk, m in done["parts"].items()))

    json.dump(g, open(os.path.join(mod_dir, "changes", "gamedb.json"), "w", encoding="utf-8"), indent=1)
    meta["provides"] = importer.provides(g)
    meta.setdefault("renumbered", {"cars": {}, "parts": {}})
    meta["renumbered"]["cars"].update({str(a): b for a, b in done["cars"].items()})
    for tk, m in done["parts"].items():
        meta["renumbered"]["parts"].setdefault(tk, {}).update({str(a): b for a, b in m.items()})
    json.dump(meta, open(os.path.join(mod_dir, "mod.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return done


def _column_values(mod_dirs, t, k):
    out = []
    for d in mod_dirs:
        _, g = _load(d)
        ch = (g or {}).get("tables", {}).get(t)
        if ch and k in ch["columns"]:
            i = ch["columns"].index(k)
            out += [r[i] for r in ch.get("insert", []) if _int(r[i])]
    return out


def importer_warning(meta, text):
    meta.setdefault("warnings", []).append(text)


def _file_rec(meta, rel):
    return next((f for f in meta["files"] if f["path"].lower() == rel.lower()), None)


def _rewrite_file(mod_dir, rec, data):
    p = os.path.join(mod_dir, *rec["file"].split("/"))
    open(p + ".new", "wb").write(data)
    os.replace(p + ".new", p)
    rec["sha256"] = importer.sha256(data)


def _carbin(mod_dir, meta, name, old, new, remapped, log):
    rec = _file_rec(meta, "cars/%s.zip" % name)
    if rec is None:
        log("  (no car zip cars/%s.zip in the mod - only the database moved)" % name)
        return
    zp = os.path.join(mod_dir, *rec["file"].split("/"))
    base = open(zp, "rb").read()
    z = zipfile.ZipFile(io.BytesIO(base))
    changes = {}
    for i in z.infolist():
        if not i.filename.lower().endswith(".carbin"):
            continue
        d = bytearray(z.read(i))
        if len(d) >= 23 and struct.unpack_from("<I", d, 19)[0] == old:
            struct.pack_into("<I", d, 19, new)
        n, left = 0, 0
        for k in range(1000):
            o = struct.pack("<I", old * 1000 + k)
            if o not in d:
                continue
            if old * 1000 + k not in remapped:
                left += d.count(o)
                continue
            nb = struct.pack("<I", remapped[old * 1000 + k])
            for m in [m.start() for m in re.finditer(re.escape(o), bytes(d))]:
                d[m:m + 4] = nb
                n += 1
        changes[i.filename] = bytes(d)
        log("  %s: model id + %d part id(s) moved%s" % (i.filename, n, ", %d id-like value(s) that the database does "
                                                         "not define left as they are" % left if left else ""))
    if changes:
        _rewrite_file(mod_dir, rec, forzazip.patched(base, changes))


def _thumbnail(mod_dir, meta, thumb, old, new, log):
    """rename the car's thumbnail files when the mod ships them under names with the old id. The game resolves
    Thumbnail_<id>.png to the files next to it that start with that name (Thumbnail_<id>_Big.swatchbin, ...)."""
    if not isinstance(thumb, str) or ("_%d." % old) not in thumb:
        return thumb
    rel = re.sub(r"(?i)^game:/media/", "", thumb.replace("\\", "/"))
    folder, base = rel.rsplit("/", 1) if "/" in rel else ("", rel)
    stem = base.rsplit(".", 1)[0].lower()
    ren = lambda n: n.replace("_%d" % old, "_%d" % new, 1)

    def matches(name):
        n = name.lower()
        return n.startswith(stem) and n[len(stem):len(stem) + 1] in (".", "_")
    hit = False
    for e in meta["entries"]:                       # entries of a stock zip (e.g. stripped/RC0.zip)
        d, b = e["entry"].rsplit("/", 1) if "/" in e["entry"] else ("", e["entry"])
        if "/".join(x for x in (e["zip"][:-4], d) if x).lower() == folder.lower() and matches(b):
            src = os.path.join(mod_dir, *e["file"].split("/"))
            e["entry"] = "/".join(x for x in (d, ren(b)) if x)
            e["file"] = "entries/%s/%s" % (e["zip"], e["entry"])
            dst = os.path.join(mod_dir, *e["file"].split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            log("  thumbnail -> %s" % e["entry"])
            hit = True
    for f in meta["files"]:                         # inside a zip the mod adds (its car zip, a new RC zip)
        zdir = f["path"][:-4].lower()
        if not f["path"].lower().endswith(".zip") or not (folder.lower() + "/").startswith(zdir + "/"):
            continue
        inner = folder[len(zdir) + 1:].lower()
        zp = os.path.join(mod_dir, *f["file"].split("/"))
        data = open(zp, "rb").read()
        z = zipfile.ZipFile(io.BytesIO(data))
        changes = {}
        for n in z.namelist():
            d, b = n.rsplit("/", 1) if "/" in n else ("", n)
            if d.lower() == inner and matches(b):
                changes[n] = None
                changes["/".join(x for x in (d, ren(b)) if x)] = z.read(n)
        if changes:
            _rewrite_file(mod_dir, f, forzazip.patched(data, changes))
            log("  thumbnail -> %s (in %s)" % (", ".join(k for k, v in changes.items() if v is not None), f["path"]))
            hit = True
    return thumb.replace("_%d." % old, "_%d." % new) if hit else thumb   # a file the mod does not ship: kept


def check(mod_dir, stock, others):
    """the renumbered database changes apply on top of the other mods' (on a copy of the stock database)"""
    fd, tmp = tempfile.mkstemp(suffix=".slt")
    os.close(fd)
    try:
        shutil.copyfile(stock.path(DB), tmp)
        shared = {}
        from .builder import RENUMBER
        for o in others:
            _, og = _load(o)
            if og:
                try:
                    apply.gamedb_apply(tmp, og, RENUMBER, [], shared, os.path.basename(o))
                except apply.Conflict:
                    pass                            # the other mods clash among themselves: not this mod's doing
        _, g = _load(mod_dir)
        try:
            apply.gamedb_apply(tmp, g, RENUMBER, [], shared, "this mod")
        except apply.Conflict as e:
            raise RenumberError("after renumbering, the mod still clashes: %s" % e)
        c = sqlite3.connect(tmp)
        ok = c.execute("pragma integrity_check").fetchone()[0]
        c.close()
        if ok != "ok":
            raise RenumberError("integrity check failed after renumbering: %s" % ok)
    finally:
        os.remove(tmp)
