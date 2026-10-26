"""Add one import (dll name + function) to a 64-bit PE file - how FH2Patch's loader is hooked into Azimuth.

FH2Patch's host (FH2Patch.dll, export FH2PatchInit) is loaded by giving Azimuth's data\\bin\\azimuth-tls.dll one more
import. Its injector adds a section ".fh2patc" laid out as
  +0x00  the import descriptors: the original ones, the new one, a zero descriptor
  +....  the dll name (FH2Patch's injector writes the absolute path of data\\bin\\FH2Patch.dll, because the loader
         does not look in the importing dll's own folder)
  then 8-aligned: the lookup table (hint/name rva, 0), the address table (the same), the hint/name (u16 0 + name)
and points the import directory at it. add_import() writes the same layout, so the result matches that injector's.
"""
import struct


class PEError(Exception):
    pass


def _align(v, a):
    return (v + a - 1) // a * a


def add_import(d, dll_name, func):
    """PE bytes with dll_name!func imported (as the first new import, after the existing ones)"""
    d = bytearray(d)
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        raise PEError("not a PE file")
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    opt = pe + 24
    if struct.unpack_from("<H", d, opt)[0] != 0x20B:
        raise PEError("not a 64-bit PE file")
    sect_align, file_align = struct.unpack_from("<II", d, opt + 32)
    size_hdr = struct.unpack_from("<I", d, opt + 60)[0]
    so = opt + struct.unpack_from("<H", d, pe + 20)[0]
    if so + 40 * (nsec + 1) > size_hdr:
        raise PEError("no room for another section header")
    secs = [struct.unpack_from("<8sIIII", d, so + 40 * k) for k in range(nsec)]

    def raw(rva):
        for _, vs, va, rs, ra in secs:
            if va <= rva < va + max(vs, rs):
                return ra + rva - va
        raise PEError("rva %x not in a section" % rva)

    dd = opt + 112
    imp_rva = struct.unpack_from("<I", d, dd + 8)[0]
    descs = []
    r = raw(imp_rva)
    while True:
        desc = bytes(d[r:r + 20])
        if desc == bytes(20):
            break
        descs.append(desc)
        r += 20
    last = secs[-1]
    new_va = _align(last[2] + max(last[1], last[3]), sect_align)
    new_raw = _align(last[4] + last[3], file_align)
    if len(d) > new_raw:
        raise PEError("data after the last section (overlay / signature) - not supported")
    name = dll_name.encode("latin-1") + b"\0"
    off_name = 20 * (len(descs) + 2)
    off_ilt = _align(off_name + len(name), 16)
    off_iat = off_ilt + 16
    off_hint = off_iat + 16
    hint = b"\0\0" + func.encode("latin-1") + b"\0"
    vsize = _align(off_hint + len(hint), 16)
    body = bytearray(vsize)
    for k, desc in enumerate(descs):
        body[20 * k:20 * k + 20] = desc
    struct.pack_into("<IIIII", body, 20 * len(descs), new_va + off_ilt, 0, 0, new_va + off_name, new_va + off_iat)
    body[off_name:off_name + len(name)] = name
    struct.pack_into("<QQ", body, off_ilt, new_va + off_hint, 0)
    struct.pack_into("<QQ", body, off_iat, new_va + off_hint, 0)
    body[off_hint:off_hint + len(hint)] = hint
    rsize = _align(vsize, file_align)
    struct.pack_into("<8sIIIIIIHHI", d, so + 40 * nsec, b".fh2patc", vsize, new_va, rsize, new_raw, 0, 0, 0, 0,
                     0xC0000040)
    struct.pack_into("<H", d, pe + 6, nsec + 1)
    struct.pack_into("<I", d, opt + 56, _align(new_va + vsize, sect_align))       # SizeOfImage
    struct.pack_into("<II", d, dd + 8, new_va, 20 * (len(descs) + 1))             # import directory
    struct.pack_into("<II", d, dd + 11 * 8, 0, 0)                                  # no bound imports
    out = bytes(d[:new_raw]) + bytes(new_raw - len(d)) + bytes(body) + bytes(rsize - vsize)
    return out


def imports(d):
    """[(dll name, [functions])] of a 64-bit PE"""
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    opt = pe + 24
    so = opt + struct.unpack_from("<H", d, pe + 20)[0]
    secs = [struct.unpack_from("<8sIIII", d, so + 40 * k) for k in range(nsec)]

    def raw(rva):
        for _, vs, va, rs, ra in secs:
            if va <= rva < va + max(vs, rs):
                return ra + rva - va
        raise PEError("rva %x not in a section" % rva)

    def cstr(rva):
        r = raw(rva)
        return d[r:d.index(b"\0", r)].decode("latin-1")

    out = []
    r = raw(struct.unpack_from("<I", d, opt + 112 + 8)[0])
    while True:
        ilt, _, _, nm, iat = struct.unpack_from("<IIIII", d, r)
        if not nm:
            break
        fs, t = [], raw(ilt or iat)
        while True:
            v = struct.unpack_from("<Q", d, t)[0]
            if not v:
                break
            fs.append("#%d" % (v & 0xFFFF) if v >> 63 else cstr(v + 2))
            t += 8
        out.append((cstr(nm), fs))
        r += 20
    return out
