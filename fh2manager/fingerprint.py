"""Stock-game fingerprints: the size and SHA-256 of every file in an unmodified game's Mount folder.

Shipped with the tool as fh2manager/data/stock_fh2_1.0.0.64.json (hashes only, no game data). With it the tool can tell,
file by file, whether a game folder still holds the original, without needing a second copy of the game.
  python -m fh2manager fingerprint --stock <unmodified game> --out <json>
"""
import hashlib
import json
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT = os.path.join(DATA, "stock_fh2_1.0.0.64.json")


def sha256_file(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def make(stock, out, progress=None):
    if os.path.isdir(os.path.join(stock, "Mount")):
        stock = os.path.join(stock, "Mount")
    files = {}
    for root, _, names in os.walk(stock):
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, stock).replace(os.sep, "/")
            files[rel.lower()] = [os.path.getsize(p), sha256_file(p), rel]
            if progress:
                progress(len(files), rel)
    data = {"game": "Forza Horizon 2 PC 1.0.0.64", "root": "Mount", "files": dict(sorted(files.items()))}
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    json.dump(data, open(out, "w", encoding="utf-8"), separators=(",", ":"))
    return data


class Fingerprints:
    """lookup by Mount-relative path (any case / slash style)"""

    def __init__(self, path=DEFAULT):
        d = json.load(open(path, encoding="utf-8"))
        self.game = d["game"]
        self.files = d["files"]

    @staticmethod
    def norm(rel):
        return rel.replace("\\", "/").lower()

    def has(self, rel):
        return self.norm(rel) in self.files

    def size(self, rel):
        return self.files[self.norm(rel)][0]

    def sha256(self, rel):
        return self.files[self.norm(rel)][1]

    def spelled(self, rel):
        return self.files[self.norm(rel)][2]

    def matches(self, rel, path):
        """the file at path is the stock version of rel (size first, then the hash)"""
        f = self.files.get(self.norm(rel))
        return bool(f) and os.path.getsize(path) == f[0] and sha256_file(path) == f[1]
