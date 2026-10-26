"""gamedbRC.slt (SQLite) row-level diff: a mod's database against the stock one.

Tables with a primary key: insert / update (only the changed columns, with the base values for conflict checks) /
delete, by key. Tables without one: whole rows added / removed (a multiset). Values are JSON: int, float, str, null,
blobs as {"b64": "..."}.
"""
import base64
import collections
import sqlite3


def enc(v):
    if isinstance(v, bytes):
        return {"b64": base64.b64encode(v).decode("ascii")}
    return v


def dec(v):
    if isinstance(v, dict) and "b64" in v:
        return base64.b64decode(v["b64"])
    return v


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f").replace("#", "%23"), uri=True)


def schema(c):
    """{table: (create sql, [columns], [primary key columns])}"""
    out = {}
    for name, sql in c.execute("select name, sql from sqlite_master where type='table'"):
        info = list(c.execute('pragma table_info("%s")' % name))
        cols = [r[1] for r in info]
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        out[name] = (sql, cols, pk)
    return out


def objects(c):
    """{name: (type, sql)} of the views, triggers and explicit indexes"""
    return {n: (t, s) for t, n, s in c.execute("select type, name, sql from sqlite_master "
                                               "where type in ('view', 'trigger', 'index') and sql is not null")}


def _norm_sql(s):
    return " ".join(s.split())


def diff(base_path, mod_path):
    """{"tables": {table: {...}}, "new_tables": {table: sql}, "dropped_tables": [table],
        "renamed_tables": {old: new}, "objects": {"add": {name: {"type", "sql"}}, "drop": [name]}}
    (only what differs). A dropped table is a rename when a new table has the same columns and at least half of its
    rows (the row changes are then against the old table)."""
    b, m = open_ro(base_path), open_ro(mod_path)
    try:
        return _diff(b, m)
    finally:
        b.close()
        m.close()


def _diff(b, m):
    sb, sm = schema(b), schema(m)
    ob, om = objects(b), objects(m)
    gone, new = sorted(set(sb) - set(sm)), sorted(set(sm) - set(sb))
    renamed = {}
    for d in gone:
        drows = set(b.execute('select * from "%s"' % d).fetchall())
        best = None
        for n in new:
            if n in renamed.values() or sm[n][1] != sb[d][1] or sm[n][2] != sb[d][2]:
                continue
            keep = len(drows & set(m.execute('select * from "%s"' % n).fetchall()))
            if drows and keep * 2 >= len(drows) and (best is None or keep > best[1]):
                best = (n, keep)
        if best:
            renamed[d] = best[0]
    out = {"tables": {}, "new_tables": {}, "dropped_tables": [d for d in gone if d not in renamed],
           "renamed_tables": renamed,
           "objects": {"add": {n: {"type": om[n][0], "sql": om[n][1]} for n in sorted(om)
                               if n not in ob or _norm_sql(ob[n][1]) != _norm_sql(om[n][1])},
                       "drop": sorted(n for n in ob if n not in om or _norm_sql(ob[n][1]) != _norm_sql(om[n][1]))}}
    source = {n: d for d, n in renamed.items()}
    for t in sorted(sm):
        sql, cols, pk = sm[t]
        if t not in sb and t not in source:
            out["new_tables"][t] = sql
            rows = m.execute('select * from "%s"' % t).fetchall()
            out["tables"][t] = {"columns": cols, "key": pk, "insert": [[enc(v) for v in r] for r in rows]}
            continue
        bt = source.get(t, t)
        if sb[bt][1] != cols:
            raise ValueError("table %s: columns differ from stock (%s vs %s); column changes are not supported"
                             % (t, cols, sb[bt][1]))
        brows = b.execute('select * from "%s"' % bt).fetchall()
        mrows = m.execute('select * from "%s"' % t).fetchall()
        if brows == mrows:
            continue
        ch = {"columns": cols, "key": pk}
        if pk:
            ki = [cols.index(k) for k in pk]
            bk = {tuple(r[i] for i in ki): r for r in brows}
            mk = {tuple(r[i] for i in ki): r for r in mrows}
            ins = [[enc(v) for v in r] for k, r in mk.items() if k not in bk]
            upd = []
            for k, r in mk.items():
                if k in bk and bk[k] != r:
                    diffc = [i for i in range(len(cols)) if r[i] != bk[k][i]]
                    upd.append({"key": [enc(v) for v in k],
                                "set": {cols[i]: enc(r[i]) for i in diffc},
                                "base": {cols[i]: enc(bk[k][i]) for i in diffc}})
            dele = [[enc(v) for v in k] for k in bk if k not in mk]
            for name, val in (("insert", ins), ("update", upd), ("delete", dele)):
                if val:
                    ch[name] = val
        else:
            cb, cm = collections.Counter(brows), collections.Counter(mrows)
            add = [[enc(v) for v in r] for r, n in (cm - cb).items() for _ in range(n)]
            rem = [[enc(v) for v in r] for r, n in (cb - cm).items() for _ in range(n)]
            if add:
                ch["insert"] = add
            if rem:
                ch["remove"] = rem
        if len(ch) > 2:
            out["tables"][t] = ch
    return out


def is_empty(d):
    """no difference at all"""
    o = d.get("objects", {})
    return not (d["tables"] or d["new_tables"] or d["dropped_tables"] or d.get("renamed_tables")
                or o.get("add") or o.get("drop"))


def structure(d):
    """human-readable list of the structure changes (renamed / added / removed tables, views, triggers, indexes)"""
    out = ["renames table %s to %s" % kv for kv in d.get("renamed_tables", {}).items()]
    out += ["adds table %s" % t for t in d["new_tables"]]
    out += ["removes table %s" % t for t in d["dropped_tables"]]
    o = d.get("objects", {})
    out += ["adds %s %s" % (v["type"], n) for n, v in o.get("add", {}).items()]
    out += ["removes / replaces %s" % n for n in o.get("drop", [])]
    return out


def summary(d):
    """{table: "+inserted ~updated -deleted"} of a diff"""
    out = {}
    for t, ch in d["tables"].items():
        out[t] = "+%d ~%d -%d" % (len(ch.get("insert", [])), len(ch.get("update", [])),
                                  len(ch.get("delete", [])) + len(ch.get("remove", [])))
    return out
