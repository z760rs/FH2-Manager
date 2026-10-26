"""FH2 media\\zipmanifest.xml: every media zip the game mounts, with its central directory position.

  <Zip version="1" path="game:\\media\\cars\\abc.zip" priority="80" dirstart="..." dirsize="..." direntries="..." />
dirstart / dirsize / direntries describe the zip itself (dirsize = central directory + end record, to the end of the
file), so a mod only needs to say which zips are listed; the numbers are recomputed from the final zip files.
"""
import re
import struct

LINE = re.compile(r'<Zip\s+([^>]*?)/>')
ATTR = re.compile(r'(\w+)="([^"]*)"')


def parse(text):
    """{lower-case game path: {attribute: value}} in file order"""
    out = {}
    for m in LINE.finditer(text):
        a = dict(ATTR.findall(m.group(1)))
        out[a["path"].lower()] = a
    return out


def central_dir(data):
    """(dirstart, dirsize, direntries) of zip bytes"""
    e = data.rfind(b"PK\x05\x06")
    assert e >= 0, "no end of central directory record"
    entries, _, start = struct.unpack_from("<HII", data, e + 10)
    return start, len(data) - start, entries


def game_path(media_rel):
    """game:\\media\\... path of a file given relative to the media folder"""
    return "game:\\media\\" + media_rel.replace("/", "\\").lower()
