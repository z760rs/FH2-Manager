"""Apply a change-based mod's changes to stock data (used to verify an import, and by the builder).

Each function takes the base data and one mod's change set and returns the new data; conflicts (a change whose
"base" no longer matches) raise Conflict.
"""
import collections
import json
import sqlite3

from . import gamedb
from .formats import fmodstrings, strtable


class Conflict(Exception):
    pass


def gamedb_apply(db_path, d, renumber=None, notes=None, shared=None, who=None):
    """apply a gamedb.json change set to the SQLite file at db_path, in one transaction.
    renumber: {table: integer key column} whose key nothing else refers to - an insert that collides there gets the
    next free key instead of failing (two mods that each took "highest stock id + 1"); notes collects what changed.
    shared: one dict for every mod of a build; a row an earlier mod inserted identically is added once (cars converted
    from the same game share engines / parts), and a clash names that mod. who: this mod's name."""
    if shared is None:
        shared = {}
    owners = shared.setdefault("owners", {})       # (table, key) -> mod that inserted it
    loose = shared.setdefault("loose", {})         # tables without a key: table -> Counter of rows earlier mods added
    same = 0
    c = sqlite3.connect(db_path)
    try:
        master = lambda: {n: t for t, n in c.execute("select type, name from sqlite_master")}
        objs = d.get("objects", {"add": {}, "drop": []})
        for n in objs["drop"]:                      # views / triggers / indexes first: renames below would rewrite them
            t = master().get(n)
            if t not in ("view", "trigger", "index"):
                raise Conflict("database object %s to replace is missing" % n)
            c.execute('drop %s "%s"' % (t, n))
        for old, new in d.get("renamed_tables", {}).items():
            have = master()
            if have.get(old) != "table":
                raise Conflict("table %s, which the mod renames to %s, is missing" % (old, new))
            if new in have:
                raise Conflict("table %s, which the mod renames %s to, exists already" % (new, old))
            c.execute('alter table "%s" rename to "%s"' % (old, new))
        for t, sql in d.get("new_tables", {}).items():
            if t in master():
                raise Conflict("table %s, which the mod adds, exists already" % t)
            c.execute(sql)
        for t, ch in d["tables"].items():
            cols, key = ch["columns"], ch["key"]
            q = lambda n: '"%s"' % n
            mine = collections.Counter()
            for r in ch.get("insert", []):
                vals = [gamedb.dec(v) for v in r]
                if not key:
                    row = tuple(json.dumps(v, sort_keys=True) for v in r)
                    mine[row] += 1
                    if mine[row] <= loose.get(t, {}).get(row, 0):
                        same += 1                   # an earlier mod added this very row
                        continue
                kv = tuple(vals[cols.index(k)] for k in key)
                try:
                    c.execute('insert into %s values (%s)' % (q(t), ",".join("?" * len(r))), vals)
                    if key:
                        owners[(t, kv)] = who
                except sqlite3.IntegrityError as e:
                    if key:
                        have = c.execute("select * from %s where %s" % (q(t), " and ".join("%s=?" % q(k) for k in key)),
                                         list(kv)).fetchone()
                        if have is not None and list(have) == vals:
                            same += 1               # the same row, already added by an earlier mod
                            continue
                    col = (renumber or {}).get(t)
                    if col is None or key != [col]:
                        k = ", ".join("%s %s" % (n, v) for n, v in zip(key, kv)) or str(r[:3])
                        if key and (t, kv) in owners:
                            raise Conflict("%s %s is added by both %s and %s, with different contents"
                                           % (t, k, owners[(t, kv)], who or "this mod"))
                        raise Conflict("%s: insert %s: %s" % (t, k, e))
                    i = cols.index(col)
                    old = vals[i]
                    vals[i] = c.execute('select max("%s") from %s' % (col, q(t))).fetchone()[0] + 1
                    c.execute('insert into %s values (%s)' % (q(t), ",".join("?" * len(vals))), vals)
                    if notes is not None:
                        notes.append("%s.%s %s was taken - inserted as %s" % (t, col, old, vals[i]))
            if not key:
                merged = loose.setdefault(t, collections.Counter())
                for row, n in mine.items():
                    merged[row] = max(merged[row], n)
            for u in ch.get("update", []):
                where = " and ".join("%s=?" % q(k) for k in key)
                kv = [gamedb.dec(v) for v in u["key"]]
                row = c.execute("select %s from %s where %s" % (",".join(q(k) for k in u["base"]), q(t), where), kv).fetchone()
                if row is None or list(row) != [gamedb.dec(v) for v in u["base"].values()]:
                    raise Conflict("%s %s: the row is not what the mod expects" % (t, u["key"]))
                c.execute("update %s set %s where %s" % (q(t), ",".join("%s=?" % q(k) for k in u["set"]), where),
                          [gamedb.dec(v) for v in u["set"].values()] + kv)
            for k in ch.get("delete", []):
                n = c.execute("delete from %s where %s" % (q(t), " and ".join("%s=?" % q(x) for x in key)),
                              [gamedb.dec(v) for v in k]).rowcount
                if n != 1:
                    raise Conflict("%s %s: row to delete is missing" % (t, k))
            for r in ch.get("remove", []):
                vals = [gamedb.dec(v) for v in r]
                where = " and ".join("%s is ?" % q(cn) for cn in cols)
                rid = c.execute("select rowid from %s where %s limit 1" % (q(t), where), vals).fetchone()
                if rid is None:
                    raise Conflict("%s: row to remove is missing" % t)
                c.execute("delete from %s where rowid=?" % q(t), rid)
        for t in d.get("dropped_tables", []):
            if master().get(t) != "table":
                raise Conflict("table %s, which the mod removes, is missing" % t)
            c.execute('drop table "%s"' % t)
        for n, o in objs["add"].items():
            if n in master():
                raise Conflict("database %s %s, which the mod adds, exists already" % (o["type"], n))
            c.execute(o["sql"])
        c.commit()
        if same and notes is not None:
            notes.append("%d database row(s) were already added, identical, by another mod - added once" % same)
    finally:
        c.close()


def strings_apply(data, d):
    """apply one table's {"add", "change", "remove"} to .str bytes"""
    t = strtable.parse(data)
    rows = {r["tag"]: r for r in t["rows"]}
    for r in d["remove"]:
        if rows.get(r["tag"]) != r:
            raise Conflict("string 0x%X to remove is not what the mod expects" % r["tag"])
        del rows[r["tag"]]
    for ch in d["change"]:
        if rows.get(ch["tag"]) != ch["base"]:
            raise Conflict("string 0x%X is not what the mod expects" % ch["tag"])
        rows[ch["tag"]] = ch["new"]
    for r in d["add"]:
        if r["tag"] in rows and all(rows[r["tag"]].get(k) == r.get(k) for k in ("text", "name")):
            continue                                # the same string, added by another mod too
        if r["tag"] in rows:
            raise Conflict("string tag 0x%X is already used (%r)" % (r["tag"], rows[r["tag"]]["text"]))
        rows[r["tag"]] = r
    t["rows"] = list(rows.values())
    return strtable.build(t)


def fmod_apply(bank, d):
    """apply {"add", "change", "remove"} to a strings bank"""
    paths = fmodstrings.bank_paths(bank)
    for p in d["remove"]:
        if p not in paths:
            raise Conflict("sound path %s to remove is missing" % p)
        del paths[p]
    for p, ch in d["change"].items():
        if paths.get(p) != ch["base"]:
            raise Conflict("sound path %s is not what the mod expects" % p)
        paths[p] = ch["new"]
    for p, g in d["add"].items():
        if p in paths and paths[p] != g:
            raise Conflict("sound path %s already exists with another GUID" % p)
        paths[p] = g
    return fmodstrings.bank_with_paths(bank, paths)
