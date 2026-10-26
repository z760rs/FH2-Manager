"""FH2 LSB2 string tables (media/stripped/stringtables/<lang>.zip, entries *.str).

Layout (all little endian):
  header  'LSB2', u32 0, u32 512, u32 A offset (36), u32 4, u32 36, u32 B offset, u32 tail offset (0 = no tail),
          u32 C offset
  A       u32 size (incl. this 8-byte head), u32 count; count x (u16 tag, u32 char offset), sorted by tag;
          u16 0xffff, u32 pool length in chars; the UTF-16LE NUL-terminated strings; u16 0
  B       u32 size, u32 count; count x (u16 tag, u16 aux, u32 byte offset), same tag order; the NUL-terminated
          ASCII key names (e.g. IDS_ModelShort_11428); no padding
  tail    an opaque block (build comment), when present
  C       u32 size, u32 count; count x u16: every tag once, in authoring order (the source row order, not tag order)
The database references a string as "_&<u32>" with u32 = (table prefix << 16) | tag.
"""
import struct


def parse(d):
    h = struct.unpack_from("<4s8I", d, 0)
    assert h[0] == b"LSB2", h
    ao, bo, to, co = h[3], h[6], h[7], h[8]
    _, an = struct.unpack_from("<II", d, ao)
    A = [struct.unpack_from("<HI", d, ao + 8 + 6 * k) for k in range(an)]
    ps = ao + 8 + 6 * an + 6
    strs = []
    for _, off in A:
        o = e = ps + off * 2
        while d[e:e + 2] != b"\0\0":
            e += 2
        strs.append(d[o:e].decode("utf-16-le"))
    bsz, bn = struct.unpack_from("<II", d, bo)
    B = [struct.unpack_from("<HHI", d, bo + 8 + 8 * k) for k in range(bn)]
    ps2 = bo + 8 + 8 * bn
    names = {}
    for tag, aux, off in B:
        e = d.index(b"\0", ps2 + off)
        names[tag] = (aux, d[ps2 + off:e].decode("latin-1"))
    # Some third-party tools grow A but leave B, the tail and C as they were (stale offsets, missing names). The game
    # only needs A (tag -> text); B / tail / C are used when they are consistent and rebuilt otherwise.
    malformed = [a[0] for a in A] != [b[0] for b in B]
    b_end = bo + bsz
    tail = None
    if to:
        if b_end <= to <= co <= len(d):
            tail = d[to:co]
        else:
            malformed = True
    order, rest = None, b""
    if co >= b_end and co + 8 <= len(d):
        csz, cn = struct.unpack_from("<II", d, co)
        if cn == an and co + 8 + 2 * cn <= len(d):
            order = list(struct.unpack_from("<%dH" % cn, d, co + 8))
            rest = d[co + csz:]
    if order is None or sorted(order) != sorted(a[0] for a in A):
        malformed, order = True, [a[0] for a in A]
    rows = [{"tag": A[k][0], "text": strs[k], "aux": names.get(A[k][0], (0, ""))[0],
             "name": names.get(A[k][0], (0, ""))[1]} for k in range(an)]
    if malformed:
        return {"hdr": h, "rows": rows, "order": order, "tail": tail, "rest": rest,
                "text_order": [a[0] for a in sorted(A, key=lambda a: a[1])],
                "name_order": [b[0] for b in sorted(B, key=lambda b: b[2])], "malformed": True}
    # the pools' own layout order (kept so an unchanged table rebuilds byte for byte)
    text_order = [a[0] for a in sorted(A, key=lambda a: a[1])]
    name_order = [b[0] for b in sorted(B, key=lambda b: b[2])]
    return {"hdr": h, "rows": rows, "order": order, "tail": tail, "rest": d[co + csz:],
            "text_order": text_order, "name_order": name_order}


def _layout(rows, keep):
    """tags in pool order: the parsed order for rows that still exist, then new rows in tag order"""
    tags = {r["tag"] for r in rows}
    out = [g for g in keep if g in tags]
    have = set(out)
    return out + [r["tag"] for r in rows if r["tag"] not in have]


def build(t):
    h = t["hdr"]
    rows = sorted(t["rows"], key=lambda r: r["tag"])
    by = {r["tag"]: r for r in rows}
    pool, off = bytearray(), {}
    for g in _layout(rows, t.get("text_order", [])):
        off[g] = len(pool) // 2
        pool += by[g]["text"].encode("utf-16-le") + b"\0\0"
    A = b"".join(struct.pack("<HI", r["tag"], off[r["tag"]]) for r in rows)
    A += struct.pack("<HI", 0xFFFF, len(pool) // 2) + bytes(pool) + b"\0\0"
    A = struct.pack("<II", 8 + len(A), len(rows)) + A
    npool, noff = bytearray(), {}
    for g in _layout(rows, t.get("name_order", [])):
        noff[g] = len(npool)
        npool += by[g]["name"].encode("latin-1") + b"\0"
    B = b"".join(struct.pack("<HHI", r["tag"], r["aux"], noff[r["tag"]]) for r in rows) + bytes(npool)
    B = struct.pack("<II", 8 + len(B), len(rows)) + B
    order = _layout(rows, t["order"])                                    # new rows: appended in tag order
    C = struct.pack("<II", 8 + 2 * len(order), len(order)) + struct.pack("<%dH" % len(order), *order)
    ao = h[3]
    bo = ao + len(A)
    tail = t["tail"]
    to = bo + len(B) if tail is not None else 0
    co = (to + len(tail)) if tail is not None else bo + len(B)
    head = struct.pack("<4s8I", b"LSB2", h[1], h[2], ao, h[4], h[5], bo, to, co)
    return head + bytes(ao - 36) + A + B + (tail or b"") + C + t["rest"]


def supported(d):
    """True when this module rebuilds the table byte for byte (2086 of the 2170 stock tables; the rest, e.g.
    appManifest / List_AddOnTable / TitleUpdate2, use another header and are handled as whole entries)"""
    try:
        return build(parse(d)) == d
    except Exception:
        return False


def diff(base, mod):
    """{"add": [row], "change": [{"tag", "base", "new"}], "remove": [row]}; the authoring order is not compared"""
    b = {r["tag"]: r for r in base["rows"]}
    m = {r["tag"]: r for r in mod["rows"]}
    return {"add": [m[t] for t in sorted(m) if t not in b],
            "change": [{"tag": t, "base": b[t], "new": m[t]} for t in sorted(m) if t in b and m[t] != b[t]],
            "remove": [b[t] for t in sorted(b) if t not in m]}
