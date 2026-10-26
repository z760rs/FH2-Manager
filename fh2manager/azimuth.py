"""Make Azimuth (and Extra Options) accept a changed Mount\\hash.manifest.

Azimuth checks the game's file index: it keeps a table of (pointer to an expected SHA-256 as 64 hex characters,
u64 value) per checked file; for hash.manifest the value is the record count (file size / 12). A mod that adds files
needs a bigger index, so both values must change. Known layouts (raw offset of the SHA-256 text, rva of the count):
  0.9.9   0x2a1110 / 0x2a1ee0
  0.9.10  0x2b82f0 / 0x2b8b30
The layout is recognised by the count slot being preceded by a pointer to the text, so a launcher already patched this
way is accepted too.

Extra Options (winmm.dll next to azimuth.exe) checks azimuth.exe's SHA-256: the 32-byte digest is stored once in the
dll. After patching azimuth.exe, that digest is replaced with the new exe's.
"""
import hashlib
import struct

LAYOUTS = [("0.9.9", 0x2a1110, 0x2a1ee0), ("0.9.10", 0x2b82f0, 0x2b8b30)]
IMAGE_BASE = 0x140000000


class AzimuthError(Exception):
    pass


def _raw_of(d, rva):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    o = pe + 24 + struct.unpack_from("<H", d, pe + 20)[0]
    for _ in range(nsec):
        vs, va, rs, ra = struct.unpack_from("<IIII", d, o + 8)
        if va <= rva < va + max(vs, rs):
            return ra + rva - va
        o += 40
    raise ValueError("rva %x not mapped" % rva)


def _is_hex_text(d, r):
    return 0 <= r and r + 65 <= len(d) and d[r + 64] == 0 and all(chr(x) in "0123456789abcdef" for x in d[r:r + 64])


def _rva_of_raw(d, raw):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    o = pe + 24 + struct.unpack_from("<H", d, pe + 20)[0]
    for _ in range(nsec):
        vs, va, rs, ra = struct.unpack_from("<IIII", d, o + 8)
        if ra <= raw < ra + rs:
            return va + raw - ra
        o += 40
    return None


def _entries_pointing_at(d, raw):
    """raw offsets of 8-byte pointers to the text at raw"""
    import re
    rva = _rva_of_raw(d, raw)
    if rva is None:
        return []
    return [m.start() for m in re.finditer(re.escape(struct.pack("<Q", IMAGE_BASE + rva)), d)]


def _anchors():
    """(SHA-256 of the stock game exe, its size, SHA-256 of the stock hash.manifest, its record count)"""
    from .fingerprint import Fingerprints
    fp = Fingerprints()
    return (fp.sha256("forza_durango_release_final.exe"), fp.size("forza_durango_release_final.exe"),
            fp.sha256("hash.manifest"), fp.size("hash.manifest") // 12)


def layout(d):
    """(version, raw offset of the hash text, raw offset of the count) of an azimuth.exe. Known builds first; for any
    other build the table is found by its contents: the game exe's entry (its stock SHA-256 + size) is followed by the
    hash.manifest entry, or (unpatched launcher) the stock hash.manifest SHA-256 with its record count."""
    for ver, hraw, crva in LAYOUTS:
        try:
            c = _raw_of(d, crva)
            if _raw_of(d, struct.unpack_from("<Q", d, c - 8)[0] - IMAGE_BASE) == hraw and _is_hex_text(d, hraw):
                return ver, hraw, c
        except (ValueError, struct.error):
            pass
    try:
        exe_sha, exe_size, idx_sha, idx_count = _anchors()
    except (OSError, KeyError, ValueError):
        exe_sha = None
    if exe_sha:
        found = set()
        start = 0
        while True:                                 # the game exe's entry, then the next one = hash.manifest
            r = d.find(exe_sha.encode("ascii"), start)
            if r < 0:
                break
            start = r + 1
            for k in _entries_pointing_at(d, r):
                if struct.unpack_from("<Q", d, k + 8)[0] != exe_size:
                    continue
                p = struct.unpack_from("<Q", d, k + 16)[0]
                try:
                    hraw = _raw_of(d, p - IMAGE_BASE)
                except ValueError:
                    continue
                if _is_hex_text(d, hraw):
                    found.add((hraw, k + 24))
        start = 0
        while True:                                 # an unpatched launcher: the stock index hash + its count
            r = d.find(idx_sha.encode("ascii"), start)
            if r < 0:
                break
            start = r + 1
            for k in _entries_pointing_at(d, r):
                if struct.unpack_from("<Q", d, k + 8)[0] == idx_count:
                    found.add((r, k + 8))
        if len(found) == 1:
            hraw, c = found.pop()
            return "unlisted build", hraw, c
    raise AzimuthError("this azimuth.exe is a build FH2 Manager cannot read (known: %s; its file-check table was not "
                       "found) - the launcher was left unchanged" % ", ".join(v for v, _, _ in LAYOUTS))


def expected_index(exe):
    """(Azimuth version, SHA-256 hex, record count) of the hash.manifest this azimuth.exe accepts"""
    ver, h, c = layout(exe)
    return ver, exe[h:h + 64].decode("ascii"), struct.unpack_from("<Q", exe, c)[0]


def patched(exe, index):
    """azimuth.exe bytes that accept the given hash.manifest bytes"""
    if len(index) % 12:
        raise AzimuthError("hash.manifest size %d is not a multiple of 12" % len(index))
    d = bytearray(exe)
    _, h, c = layout(d)
    d[h:h + 64] = hashlib.sha256(index).hexdigest().encode("ascii")
    struct.pack_into("<Q", d, c, len(index) // 12)
    return bytes(d)


def _rsrc(d):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    o = pe + 24 + struct.unpack_from("<H", d, pe + 20)[0]
    for _ in range(nsec):
        name = d[o:o + 8].rstrip(b"\0")
        vs, va, rs, ra = struct.unpack_from("<IIII", d, o + 8)
        if name == b".rsrc":
            return ra, ra + rs
        o += 40
    return None


def tls_slot(exe):
    """raw offset of the expected SHA-256 of data\\bin\\azimuth-tls.dll (64 hex characters in the resources; Azimuth
    0.9.10 checks its own dll against it), or None for a launcher without one"""
    import re
    r = _rsrc(exe)
    if not r:
        return None
    hits = [m.start() for m in re.finditer(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", exe[r[0]:r[1]])]
    return r[0] + hits[0] if len(hits) == 1 else None


def expected_tls(exe):
    s = tls_slot(exe)
    return exe[s:s + 64].decode("ascii") if s is not None else None


def with_tls(exe, tls):
    """azimuth.exe bytes that accept this azimuth-tls.dll"""
    s = tls_slot(exe)
    if s is None:
        raise AzimuthError("this azimuth.exe has no azimuth-tls.dll check to update")
    return exe[:s] + hashlib.sha256(tls).hexdigest().encode("ascii") + exe[s + 64:]


def accepts(exe, index):
    _, sha, count = expected_index(exe)
    return sha == hashlib.sha256(index).hexdigest() and count == len(index) // 12


def eo_rehost(dll, old_exe, new_exe, also=(), digests=()):
    """Extra Options winmm.dll bytes that accept new_exe instead of old_exe. also: other azimuth.exe versions the dll
    may still expect (the original exe, when a fresh copy of Extra Options was dropped in after patching); digests:
    SHA-256 digests of more such versions (a patched exe that was replaced since)"""
    new = hashlib.sha256(new_exe).digest()
    if dll.count(new) == 1:
        return dll
    for old in [hashlib.sha256(x).digest() for x in (old_exe,) + tuple(also)] + list(digests):
        if dll.count(old) == 1:
            i = dll.find(old)
            return dll[:i] + new + dll[i + 32:]
    raise AzimuthError("winmm.dll does not carry the SHA-256 of azimuth.exe (current or original) - is it Extra "
                       "Options for this launcher?")
