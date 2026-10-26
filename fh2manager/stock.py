"""Where the tool reads the stock game from.

Two sources with the same interface (paths are relative to the game's media folder, lower case):
  FolderStock(path)        a complete, unmodified copy of the game
  GameBaseline(game, ...)  the player's own game folder: a file counts as stock only if it matches the shipped
                           fingerprint; files the tool replaced at install time are read from its originals cache; an
                           optional unmodified copy fills the gaps (stock files someone else overwrote)
  names            {rel: spelling}            every stock file under media
  has(rel), spelled(rel), read(rel), path(rel) (a real file holding the stock bytes)
  read_mount(name) stock bytes of a file directly in Mount (hash.manifest), or None
"""
import os

from .fingerprint import Fingerprints, sha256_file


class NotStock(Exception):
    """stock files that are not available (modified in the game folder, no original kept, no unmodified copy)"""

    def __init__(self, paths):
        self.paths = sorted(set(paths))
        super().__init__("these game files are modified and no original is available: %s" % ", ".join(self.paths))


class FolderStock:
    def __init__(self, path):
        if os.path.isdir(os.path.join(path, "Mount")):
            path = os.path.join(path, "Mount")
        if not os.path.isdir(os.path.join(path, "media")):
            raise SystemExit("stock: no media folder in %s" % path)
        if any(os.path.isdir(os.path.join(path, r)) for r in ("mediapc", "mediaoverride")):
            raise SystemExit("stock: %s has a mediapc / mediaoverride folder - it is not an unmodified game" % path)
        self.mount = path
        self.media = os.path.join(path, "media")
        self.names, self._paths = {}, {}
        for root, _, files in os.walk(self.media):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, self.media).replace(os.sep, "/")
                self.names[rel.lower()] = rel
                self._paths[rel.lower()] = p

    def has(self, rel):
        return rel in self.names

    def spelled(self, rel):
        return self.names[rel]

    def path(self, rel):
        return self._paths[rel]

    def read(self, rel):
        with open(self.path(rel), "rb") as f:
            return f.read()

    def mount_path(self, name):
        p = os.path.join(self.mount, *name.split("/"))
        return p if os.path.isfile(p) else None

    def read_mount(self, name):
        p = self.mount_path(name)
        return open(p, "rb").read() if p else None


class GameBaseline:
    def __init__(self, game_mount, originals, fallback=None, fingerprints=None):
        self.fp = fingerprints or Fingerprints()
        self.mount = game_mount
        self.originals = originals                  # <state>/originals, laid out like Mount
        self.fallback = fallback                    # a FolderStock-like unmodified copy, or None
        self.names = {k[len("media/"):]: v[2][len("media/"):] for k, v in self.fp.files.items() if k.startswith("media/")}
        self._cache = {}

    def _resolve(self, mount_rel):
        """real path holding the stock bytes of a Mount-relative file, or None"""
        key = mount_rel.lower()
        if key in self._cache:
            return self._cache[key]
        spelled = self.fp.spelled(key)
        found = None
        for cand in (os.path.join(self.originals, *spelled.split("/")), os.path.join(self.mount, *spelled.split("/"))):
            if os.path.isfile(cand) and self.fp.matches(key, cand):
                found = cand
                break
        if found is None and self.fallback is not None:
            cand = self.fallback.mount_path(spelled) if hasattr(self.fallback, "mount_path") else None
            if cand and self.fp.matches(key, cand):
                found = cand
        self._cache[key] = found
        return found

    def has(self, rel):
        return rel in self.names

    def spelled(self, rel):
        return self.names[rel]

    def path(self, rel):
        p = self._resolve("media/" + rel)
        if p is None:
            raise NotStock(["media/" + self.names.get(rel, rel)])
        return p

    def read(self, rel):
        with open(self.path(rel), "rb") as f:
            return f.read()

    def mount_path(self, name):
        return self._resolve(name) if self.fp.has(name) else None

    def read_mount(self, name):
        p = self.mount_path(name)
        if p is None and self.fp.has(name):
            raise NotStock([self.fp.spelled(name)])
        return open(p, "rb").read() if p else None

    def missing(self, mount_rels):
        """which of these Mount-relative stock files cannot be read as stock"""
        return [self.fp.spelled(r) for r in mount_rels if self.fp.has(r) and self._resolve(r) is None]


def as_source(x):
    return FolderStock(x) if isinstance(x, str) else x


def sha_of(path):
    return sha256_file(path)
