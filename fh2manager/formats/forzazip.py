"""FH2's native media zip layout.

Stock zips differ from what Python's zipfile writes in three ways the game relies on:
  - the central directory is sorted by upper-cased entry name (the game binary-searches it; an entry out of order
    is not found / crashes on load)
  - every CENTRAL directory record carries extra field 0x1123 (4 bytes) = the absolute offset of the entry's data
    (local header + 30 + name length + local extra length)
  - "version made by" / "version needed" are 10 and the external attributes 0
"""
import io
import struct
import zipfile


def _records(d):
    e = d.rfind(b"PK\x05\x06")
    assert e >= 0, "not a zip"
    n, size, start = struct.unpack_from("<HII", d, e + 10)
    out, o = [], start
    for _ in range(n):
        assert d[o:o + 4] == b"PK\x01\x02", "central directory record expected at 0x%x" % o
        nl, xl, cl = struct.unpack_from("<HHH", d, o + 28)
        out.append((o, 46 + nl + xl + cl, nl, xl))
        o += 46 + nl + xl + cl
    return e, start, out


def issues(d):
    """what keeps zip bytes from being native: [] when native"""
    e, start, recs = _records(d)
    names, bad = [], []
    for o, ln, nl, xl in recs:
        names.append(d[o + 46:o + 46 + nl].decode("latin-1"))
        extra = d[o + 46 + nl:o + 46 + nl + xl]
        k, found = 0, False
        while k + 4 <= len(extra):
            hid, hl = struct.unpack_from("<HH", extra, k)
            found |= hid == 0x1123
            k += 4 + hl
        if not found:
            bad.append(names[-1])
    out = []
    if any(a.upper() > b.upper() for a, b in zip(names, names[1:])):
        out.append("central directory not sorted by name")
    if bad:
        out.append("%d entries without the 0x1123 data-offset field" % len(bad))
    return out


def native(d):
    """zip bytes rewritten in the native layout (data and local headers untouched apart from version needed)"""
    d = bytearray(d)
    e, start, recs = _records(d)
    out = []
    for o, ln, nl, xl in recs:
        rec = bytearray(d[o:o + ln])
        name = bytes(rec[46:46 + nl])
        loc = struct.unpack_from("<I", rec, 42)[0]
        lnl, lxl = struct.unpack_from("<HH", d, loc + 26)
        extra, k = bytearray(), 0
        old = bytes(rec[46 + nl:46 + nl + xl])
        while k + 4 <= len(old):                      # keep other extra fields, replace any 0x1123
            hid, hl = struct.unpack_from("<HH", old, k)
            if hid != 0x1123:
                extra += old[k:k + 4 + hl]
            k += 4 + hl
        extra += struct.pack("<HHI", 0x1123, 4, loc + 30 + lnl + lxl)
        struct.pack_into("<H", rec, 30, len(extra))
        struct.pack_into("<HH", rec, 4, 10, 10)
        struct.pack_into("<I", rec, 38, 0)
        struct.pack_into("<H", d, loc + 4, 10)
        out.append((name.decode("latin-1").upper(), bytes(rec[:46 + nl]) + bytes(extra) + bytes(rec[46 + nl + xl:])))
    out.sort(key=lambda r: r[0])
    cd = b"".join(r for _, r in out)
    eocd = bytearray(d[e:])
    struct.pack_into("<I", eocd, 12, len(cd))
    struct.pack_into("<I", eocd, 16, start)
    return bytes(d[:start]) + cd + bytes(eocd)


def patched(base, changes):
    """base zip bytes with {entry name: new bytes, or None to remove} applied, in the native layout; replaced
    entries keep their compression, new ones use the zip's most common one"""
    zi = zipfile.ZipFile(io.BytesIO(base))
    infos = zi.infolist()
    low = {k.lower(): v for k, v in changes.items()}
    kinds = [i.compress_type for i in infos] or [zipfile.ZIP_STORED]
    common = max(set(kinds), key=kinds.count)
    buf = io.BytesIO()
    done = set()
    with zipfile.ZipFile(buf, "w") as zo:
        for i in infos:
            k = i.filename.lower()
            if k in low:
                done.add(k)
                if low[k] is None:
                    continue
                data = low[k]
            else:
                data = zi.read(i)
            ni = zipfile.ZipInfo(i.filename, i.date_time)
            ni.compress_type = i.compress_type
            zo.writestr(ni, data)
        for name, data in changes.items():
            if name.lower() not in done and data is not None:
                ni = zipfile.ZipInfo(name, infos[0].date_time if infos else (1980, 1, 1, 0, 0, 0))
                ni.compress_type = common
                zo.writestr(ni, data)
    return native(buf.getvalue())
