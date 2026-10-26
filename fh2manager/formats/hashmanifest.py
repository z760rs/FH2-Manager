"""FH2 Mount\\hash.manifest: the game:\\ device's file index. A file without a record cannot be opened by the game.

12-byte records, little endian, unsorted: u64 key, i32 value.
key: 32-bit shift-add-xor  h ^= (h << 5) + (h >> 2) + c  (c = signed char) over the path relative to Mount, lower
     case, backslashes; A = one pass from 5381, B = a second pass over the same string starting from A; key = B<<32 | A.
value: 0 for directories; files carry group ids shared by related files (a car's audio XML and its FMOD bank, ...).
       Its meaning is not known; a new file reuses the value of a similar stock file.
"""
import struct


def _sax(b, h):
    for c in b:
        c = c - 256 if c > 127 else c
        h = (h ^ (((h << 5) & 0xFFFFFFFF) + (h >> 2) + c)) & 0xFFFFFFFF
    return h


def key(rel_path):
    """record key of a path relative to Mount (e.g. media\\cars\\abc.zip)"""
    b = rel_path.replace("/", "\\").lower().encode("latin-1")
    a = _sax(b, 5381)
    return (_sax(b, a) << 32) | a


def parse(d):
    assert len(d) % 12 == 0, "not a hash.manifest"
    return dict(struct.unpack_from("<Qi", d, o) for o in range(0, len(d), 12))


def build(records):
    """bytes for [(key, value)] in the given order"""
    return b"".join(struct.pack("<Qi", k, v) for k, v in records)
