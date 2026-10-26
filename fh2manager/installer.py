"""Install / uninstall a build into the player's game folder, and patch the launcher.

One game folder is all that is needed. The tool keeps its state next to Mount, in <game>/fh2manager_state/:
  journal.json      what is installed: every file written (with the stock original, a backup, or nothing to put back),
                    the launcher files patched, the mods and cars
  originals/        stock files the tool replaced (checked against the shipped fingerprints before they are kept)
  backups/          files of other mods the tool was told to take over
  adopted/          Extra Options mods that were in the game folder before (see adopt())
  launcher/         the original azimuth.exe / winmm.dll
  save_backups/     copies of the player's saves, made before each install that changes the game (last 5 kept)
apply() rebuilds from stock every time, so enabling / disabling a mod is just another apply().

Safety:
  - nothing runs while the game or Azimuth is running
  - the full plan is checked before anything changes; a file the tool did not write and that is not stock is only
    replaced with take_over=True (and is then backed up)
  - originals and the journal are written before any game file is replaced; files are replaced with os.replace (no
    writing through hard links); a file changed since the tool wrote it is not touched without take_over=True
  - an install that would remove a car the save owns stops first (SaveRisk) unless allowed; the saves are copied
    before every install that changes the game
  - an install that was interrupted (journal "pending") is finished by the next apply / uninstall
"""
import re
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import azimuth, builder, importer, peimport
from .fingerprint import Fingerprints, sha256_file
from .stock import FolderStock, GameBaseline, NotStock

STATE = "fh2manager_state"
OLD_STATES = ("fh2modkit_state",)                   # the tool's earlier name: taken over on first use
GAME_EXE = "forza_durango_release_final.exe"
TLS = "data/bin/azimuth-tls.dll"


class InstallError(Exception):
    pass


class SaveRisk(InstallError):
    """the install would remove cars that the player's save owns (a save whose cars are gone can fail to load)"""

    def __init__(self, cars):
        self.cars = cars
        super().__init__("your save looks like it owns %s, which this would remove - a save whose cars are missing "
                         "can fail to load. Sell them in the game first, or keep their mod ticked."
                         % ", ".join("%s (%s)" % (c["media_name"], c["id"]) for c in cars))


SAVE_CAR = re.compile(r"^(?:BaseLivery|GarageThumbnail)_(\d+)_", re.I)   # files a save keeps per owned car
KEEP_SAVE_BACKUPS = 5


def _norm(p):
    return p.replace("\\", "/").lower()


def running():
    """names of the game / launcher processes that are running (Windows)"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return []
    return [n for n in ("azimuth.exe", GAME_EXE) if '"%s"' % n in out]


class Game:
    def __init__(self, game_dir, azimuth_dir=None, fallback=None, fingerprints=None):
        game_dir = os.path.abspath(game_dir)
        if os.path.basename(game_dir).lower() == "mount":
            game_dir = os.path.dirname(game_dir)
        self.dir = game_dir
        self.mount = os.path.join(game_dir, "Mount")
        if not os.path.isfile(os.path.join(self.mount, GAME_EXE)) or not os.path.isdir(os.path.join(self.mount, "media")):
            raise InstallError("%s does not look like the FH2 game folder (no Mount\\%s)" % (game_dir, GAME_EXE))
        self.azimuth = os.path.abspath(azimuth_dir) if azimuth_dir else None
        self.state = os.path.join(game_dir, STATE)
        for old in OLD_STATES:
            if not os.path.exists(self.state) and os.path.isdir(os.path.join(game_dir, old)):
                os.rename(os.path.join(game_dir, old), self.state)
        self.fp = fingerprints or Fingerprints()
        self.fallback = FolderStock(fallback) if isinstance(fallback, str) and fallback else fallback
        self.baseline = GameBaseline(self.mount, os.path.join(self.state, "originals"), self.fallback, self.fp)
        self.journal = self._load()

    # ------------------------------------------------------------------ state
    def _load(self):
        p = os.path.join(self.state, "journal.json")
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
        return {"tool": "fh2manager", "format": 1, "mods": [], "cars": [], "files": {}, "launcher": None,
                "adopted": {}, "removed_adopted": {}}

    def _save(self):
        os.makedirs(self.state, exist_ok=True)
        p = os.path.join(self.state, "journal.json")
        tmp = p + ".new"
        json.dump(self.journal, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        os.replace(tmp, p)

    def target(self, mount_rel):
        return os.path.join(self.mount, *mount_rel.split("/"))

    def _keep(self, src, state_rel):
        """copy a file into the state folder (never overwrites an existing kept copy)"""
        dst = os.path.join(self.state, *state_rel.split("/"))
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst + ".new")
            os.replace(dst + ".new", dst)
        return state_rel

    def _put(self, src_path, mount_rel, move=False):
        """place a file at mount_rel atomically (src moved when it is on the same volume and move=True)"""
        dst = self.target(mount_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if move:
            os.replace(src_path, dst)
        else:
            shutil.copyfile(src_path, dst + ".fh2mk_new")
            os.replace(dst + ".fh2mk_new", dst)

    def _remove(self, mount_rel):
        p = self.target(mount_rel)
        if os.path.exists(p):
            os.remove(p)
        d = os.path.dirname(p)
        while _norm(d).startswith(_norm(self.mount) + "/") and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            d = os.path.dirname(d)

    # ------------------------------------------------------------------ status
    def status(self):
        """what is installed, what blocks a build, which foreign Extra Options files are in the game folder"""
        j = self.journal
        shared = ["media/stripped/gamedbRC.slt", "media/zipmanifest.xml",
                  "media/audio/fmodanthem/durango/MasterBank.strings.bank", "hash.manifest"]
        mine = {_norm(k) for k in list(j["files"]) + list(j["adopted"])}
        foreign = []
        for root in ("mediapc", "mediaoverride"):
            base = os.path.join(self.mount, root)
            for r, _, fs in os.walk(base):
                for f in fs:
                    rel = os.path.relpath(os.path.join(r, f), self.mount).replace(os.sep, "/")
                    if _norm(rel) not in mine:
                        foreign.append(rel)
        out = {"installed": j["mods"], "cars": j["cars"], "files": len(j["files"]),
               "needs_unmodified_copy": self.baseline.missing([s for s in shared]),
               "foreign_extra_options_files": foreign, "launcher": None}
        if self.azimuth and os.path.isfile(os.path.join(self.azimuth, "azimuth.exe")):
            exe = open(os.path.join(self.azimuth, "azimuth.exe"), "rb").read()
            try:
                ver, sha, n = azimuth.expected_index(exe)
                idx = open(os.path.join(self.mount, "hash.manifest"), "rb").read()
                out["launcher"] = {"version": ver, "accepts_current_index": azimuth.accepts(exe, idx),
                                   "extra_options": os.path.isfile(os.path.join(self.azimuth, "winmm.dll"))}
            except azimuth.AzimuthError as e:
                out["launcher"] = {"error": str(e)}
        return out

    # ------------------------------------------------------------------ adopt
    def adopt(self, library, name="existing-mods", log=print):
        """import every change already in the game folder (mods installed by drag and drop, Extra Options mediapc
        files, files changed by hand) as one mod in the library. The files are backed up and from then on installed /
        removed by the tool like any other mod. Only before FH2 Manager has installed anything."""
        if self.journal["files"]:
            raise InstallError("adopt existing mods before installing anything with FH2 Manager (uninstall first)")
        if self.journal["adopted"]:
            raise InstallError("existing mods were adopted already")
        self.check_game()
        out = os.path.join(library, name)
        if os.path.exists(out):
            raise InstallError("the library already has a mod called %s" % name)
        log("comparing the game folder with the stock game (this reads every file once, a minute or two) ...")
        try:
            r = importer.import_mod(self.baseline, self.dir, out, name)
        except NotStock as e:
            shutil.rmtree(out, ignore_errors=True)
            raise InstallError("these game files were changed, and the tool has no original of them: %s\nSet an "
                               "unmodified copy of the game in Settings to adopt them." % ", ".join(e.paths))
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for rel in r["consumed"]:
            kept = self._keep(self.target(rel), "adopted/%s/%s" % (stamp, rel))
            self.journal["adopted"][rel] = {"sha256": sha256_file(self.target(rel)), "backup": kept}
            if self.fp.has(rel) and not self.fp.matches(rel, self.target(rel)):
                src = self.baseline.mount_path(self.fp.spelled(rel))  # the stock file it replaced (from the copy)
                if src:
                    self._keep(src, "originals/" + self.fp.spelled(rel))
        self._save()
        log("adopted %d changed file(s) as mod '%s'" % (len(r["consumed"]), name))
        return r

    # ------------------------------------------------------------------ game version, saves
    def check_game(self):
        """FH2 Manager knows only FH2 PC 1.0.0.64 (its stock fingerprints are for that version)"""
        exe = os.path.join(self.mount, GAME_EXE)
        if os.path.getsize(exe) != self.fp.size(GAME_EXE) or sha256_file(exe) != self.fp.sha256(GAME_EXE):
            raise InstallError("this game is not Forza Horizon 2 for PC version 1.0.0.64 (its %s differs) - FH2 "
                               "Manager supports only that version" % GAME_EXE)

    def save_dirs(self):
        """the player's save folders: <Azimuth>/data/titles/<title>/saves"""
        root = os.path.join(self.azimuth, "data", "titles") if self.azimuth else None
        if not root or not os.path.isdir(root):
            return []
        return [os.path.join(root, t, "saves") for t in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, t, "saves"))]

    def owned_cars(self):
        """car ids the saves own (every owned car has its own livery / garage picture files in the profile)"""
        ids = set()
        for d in self.save_dirs():
            for prof in os.listdir(d):
                pd = os.path.join(d, prof)
                if os.path.isdir(pd):
                    for f in os.listdir(pd):
                        m = SAVE_CAR.match(f)
                        if m:
                            ids.add(int(m.group(1)))
        return ids

    def backup_saves(self, log):
        dirs = self.save_dirs()
        if not dirs:
            return None
        root = os.path.join(self.state, "save_backups")
        base = stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        k = 1
        while os.path.exists(os.path.join(root, stamp)):    # two installs within one second
            k += 1
            stamp = "%s_%d" % (base, k)
        for d in dirs:
            title = os.path.basename(os.path.dirname(d))
            shutil.copytree(d, os.path.join(root, stamp, title, "saves"))
        for old in sorted(os.listdir(root))[:-KEEP_SAVE_BACKUPS]:
            shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        log("saves backed up (%s)" % os.path.join(STATE, "save_backups", stamp))
        return stamp

    def _save_risk(self, removed_cars, allow):
        """stop before anything changes when cars the save owns would be removed"""
        if allow or not removed_cars:
            return
        owned = self.owned_cars()
        risky = [c for c in removed_cars if c.get("id") in owned]
        if risky:
            raise SaveRisk(risky)

    # ------------------------------------------------------------------ apply
    def apply(self, mod_paths, take_over=False, log=print, allow_owned_removal=False):
        busy = running()
        if busy:
            raise InstallError("close %s first" % " and ".join(busy))
        self.check_game()
        os.makedirs(self.state, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="build_", dir=self.state)      # same volume as the game: moves are atomic
        try:
            if mod_paths:
                try:
                    report = builder.build(self.baseline, mod_paths, os.path.join(tmp, "out"))
                except NotStock as e:
                    raise InstallError(
                        "these game files were changed by something other than FH2 Manager, and the tool has no "
                        "original of them: %s\nRestore them (reinstall / repair the game), or set an unmodified copy "
                        "of the game in Settings." % ", ".join(e.paths))
                except builder.BuildError as e:
                    raise InstallError(str(e))
                outs = {_norm(o["path"]): (o["path"], os.path.join(tmp, "out", "Mount", *o["path"].split("/")),
                                           o["sha256"]) for o in report["outputs"]}
            else:
                report, outs = {"mods": [], "outputs": [], "notes": []}, {}
            return self._commit(outs, report, mod_paths, take_over, log, allow_owned_removal)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _commit(self, outs, report, mod_paths, take_over, log, allow_owned_removal=False):
        j = self.journal
        resuming = bool(j.get("pending"))               # the last install was interrupted: finish it
        if resuming:
            log("finishing an install that was interrupted")
        prev = {_norm(k): (k, v) for k, v in j["files"].items()}
        for rel, rec in j.get("pending_removals", []):  # files that interrupted install still had to take out
            prev.setdefault(_norm(rel), (rel, rec))
        adopted = {_norm(k): (k, v) for k, v in j["adopted"].items()}
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        problems, writes, removals, keeps = [], [], [], []
        new_files = {}
        for key, (rel, src, sha) in outs.items():
            t = self.target(rel)
            if key in prev:
                old_rel, rec = prev[key]
                if os.path.exists(t) and sha256_file(t) != rec["sha256"] and not take_over and not resuming:
                    problems.append("%s was changed after FH2 Manager installed it" % rel)
                new_files[old_rel] = dict(rec, sha256=sha)
            elif os.path.exists(t):
                if key in adopted and sha256_file(t) == adopted[key][1]["sha256"]:
                    new_files[rel] = {"sha256": sha, "was": "adopted", "backup": adopted[key][1]["backup"]}
                elif self.fp.has(rel) and self.fp.matches(rel, t):
                    keeps.append((t, "originals/" + self.fp.spelled(rel)))
                    new_files[rel] = {"sha256": sha, "was": "stock", "backup": "originals/" + self.fp.spelled(rel)}
                elif take_over:
                    keeps.append((t, "backups/%s/%s" % (stamp, rel)))
                    new_files[rel] = {"sha256": sha, "was": "foreign", "backup": "backups/%s/%s" % (stamp, rel)}
                else:
                    problems.append("%s belongs to another mod (not stock, not installed by FH2 Manager)" % rel)
            else:
                new_files[rel] = {"sha256": sha, "was": "new", "backup": None}
            writes.append((src, rel, sha))
        removed_adopted = dict(j.get("removed_adopted", {}))
        for key, (rel, rec) in prev.items():
            if key in outs:
                continue
            t = self.target(rel)
            if os.path.exists(t) and sha256_file(t) != rec["sha256"] and not take_over and not resuming:
                problems.append("%s was changed after FH2 Manager installed it" % rel)
            removals.append((rel, rec))
            if rec["was"] == "adopted" and key in adopted:
                removed_adopted[adopted[key][0]] = adopted[key][1]
        for key, (rel, rec) in adopted.items():
            if key not in outs and key not in prev and os.path.exists(self.target(rel)):
                removals.append((rel, {"was": "adopted-off", "backup": rec["backup"], "sha256": rec["sha256"]}))
                removed_adopted[rel] = rec
        stock_src = {}                              # an adopted mod had replaced a stock file: it comes back
        for rel, rec in removals:
            if rec["was"] in ("adopted", "adopted-off") and self.fp.has(rel):
                src = self.baseline.mount_path(self.fp.spelled(rel))
                if src is None:
                    problems.append("%s was replaced by a mod installed before FH2 Manager, and the tool has no "
                                    "original of it - set an unmodified copy of the game in Settings" % rel)
                    continue
                stock_src[rel] = src
                keeps.append((src, "originals/" + self.fp.spelled(rel)))
        self._launcher_preflight(new_files)
        if problems:
            raise InstallError("nothing was changed:\n  " + "\n  ".join(problems) +
                               "\n(Adopt existing mods first, or allow taking over other mods' files.)")
        meta = [json.load(open(os.path.join(p, "mod.json"), encoding="utf-8")) for p in mod_paths
                if os.path.isfile(os.path.join(p, "mod.json"))]
        old_cars = {c["media_name"].lower() for c in j.get("cars", [])}
        cars = [c for m in meta for c in m.get("provides", {}).get("cars", [])]
        kept_names = {c["media_name"].lower() for c in cars}
        self._save_risk([c for c in j.get("cars", []) if c["media_name"].lower() not in kept_names], allow_owned_removal)
        # 1. back up the saves, keep originals / backups, record the new state, then change the game
        if writes or removals:
            self.backup_saves(log)
        for src, state_rel in keeps:
            self._keep(src, state_rel)
        j.update(files=new_files, mods=[{"name": m["name"], "version": m.get("version")} for m in meta], cars=cars,
                 removed_adopted=removed_adopted, pending=True,
                 pending_removals=[[rel, rec] for rel, rec in removals])
        self._save()
        for rel, rec in removals:
            if rec["was"] in ("stock", "foreign"):
                self._put(os.path.join(self.state, *rec["backup"].split("/")), rel)
            elif rel in stock_src:                  # an adopted mod's file over a stock file: stock again
                self._put(stock_src[rel], rel)
            else:                                   # new, or an adopted mod's own file whose mod is now disabled
                self._remove(rel)
        same = 0
        for src, rel, sha in writes:
            t = self.target(rel)
            if os.path.exists(t) and os.path.getsize(t) == os.path.getsize(src) and sha256_file(t) == sha:
                same += 1                           # already in place: not rewritten
                continue
            self._put(src, rel, move=True)
        changed_launcher = self._launcher(log)
        j["pending"] = False
        j.pop("pending_removals", None)
        self._save()
        gone = sorted(old_cars - {c["media_name"].lower() for c in cars})
        result = {"written": len(writes) - same, "unchanged": same, "removed": len(removals), "mods": j["mods"], "launcher": j["launcher"],
                  "notes": report.get("notes", []), "cars_removed": gone}
        log("installed %s: %d file(s) written (%d already up to date), %d restored / removed%s"
            % (" + ".join(m["name"] for m in j["mods"]) or "nothing", len(writes) - same, same, len(removals),
               "; launcher updated" if changed_launcher else ""))
        if gone:
            log("WARNING: these cars are no longer installed: %s - if your save owns one, the game can fail to load "
                "it (sell them first, or enable their mod again)" % ", ".join(gone))
        return result

    # ------------------------------------------------------------------ launcher
    # The launcher record (journal "launcher") says which folder the tool patched, the original azimuth.exe /
    # winmm.dll it keeps, and what it wrote. When the Azimuth folder in the settings changes, the old folder gets its
    # originals back (if it still holds what the tool wrote) and the new folder is patched. When azimuth.exe or
    # winmm.dll were replaced by something else (an Azimuth / Extra Options update), the new files become the base:
    # the previous originals stay archived in the state folder and are never put back over a newer launcher.
    def _sha(self, data):
        return azimuth.hashlib.sha256(data).hexdigest()

    def _az_write(self, folder, rel, data):
        dst = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst + ".fh2mk_new", "wb").write(data)
        os.replace(dst + ".fh2mk_new", dst)

    def _kept(self, state_rel):
        return open(os.path.join(self.state, *state_rel.split("/")), "rb").read()

    def _keep_bytes_of(self, path, name):
        """keep the file at path as an original, filed under its own hash (versions never overwrite each other)"""
        return self._keep(path, "launcher/orig/%s/%s" % (sha256_file(path)[:16], name))

    def _keep_data(self, data, name):
        rel = "launcher/orig/%s/%s" % (self._sha(data)[:16], name)
        dst = os.path.join(self.state, *rel.split("/"))
        if not os.path.isfile(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst + ".fh2mk_new", "wb").write(data)
            os.replace(dst + ".fh2mk_new", dst)
        return rel

    def _migrate(self):
        """journals written before launcher records carried their folder / hashes"""
        j = self.journal
        rec = j.get("launcher")
        if rec and self.azimuth:
            rec.setdefault("folder", self.azimuth)
            if "base_sha256" not in rec:
                rec["base_sha256"] = self._sha(self._kept(rec["exe_original"]))
            dll_p = os.path.join(rec["folder"], "winmm.dll")
            exe_p = os.path.join(rec["folder"], "azimuth.exe")
            if "dll_sha256" not in rec and os.path.isfile(dll_p) and os.path.isfile(exe_p) \
                    and sha256_file(exe_p) == rec["exe_sha256"]:
                rec["dll_sha256"] = sha256_file(dll_p)
        fp = j.get("fh2patch")
        if fp and self.azimuth:
            fp.setdefault("folder", self.azimuth)
            if "payload" not in fp:                 # keep a copy of what was installed, to re-install elsewhere
                fp["payload"] = {}
                for rel, r in fp["files"].items():
                    p = os.path.join(fp["folder"], *rel.split("/"))
                    if rel != TLS and os.path.isfile(p) and sha256_file(p) == r["sha256"]:
                        fp["payload"][rel] = self._keep(p, "fh2patch/payload/" + rel)

    def _known_exes(self):
        """every azimuth.exe the tool has seen: the one in the folder, and all originals it keeps (an Extra Options
        winmm.dll names one of them when it was made for that build)"""
        out = []
        p = os.path.join(self.azimuth, "azimuth.exe")
        if os.path.isfile(p):
            out.append(open(p, "rb").read())
        for root, _, fs in os.walk(os.path.join(self.state, "launcher")):
            for f in fs:
                if f.lower() == "azimuth.exe":
                    out.append(open(os.path.join(root, f), "rb").read())
        return out

    def _known_digests(self):
        """SHA-256 digests of every azimuth.exe a winmm.dll in the folder may name: the ones the tool has seen, and
        the one it last wrote (replaced since by an update extracted over the folder, which keeps winmm.dll)"""
        out = [azimuth.hashlib.sha256(x).digest() for x in self._known_exes()]
        rec = self.journal.get("launcher")
        if rec and rec.get("exe_sha256"):
            out.append(bytes.fromhex(rec["exe_sha256"]))
        return out

    def launcher_check(self):
        """None when the Azimuth folder in the settings is in line with the install; else {"reason", "message",
        "old"}: "moved" (the setting points at another folder than the one patched), "updated" (its files were
        replaced, e.g. a new build), "missing" (no azimuth.exe there), "unreadable" (a build the tool cannot read)"""
        j = self.journal
        rec, fp = j.get("launcher"), j.get("fh2patch")
        if not self.azimuth or not (j["files"] or rec or fp):
            return None
        old = (rec or {}).get("folder") or (fp or {}).get("folder")
        if not os.path.isfile(os.path.join(self.azimuth, "azimuth.exe")):
            return {"reason": "missing", "old": old, "message": "no azimuth.exe in %s" % self.azimuth}
        if old and _norm(old) != _norm(self.azimuth):
            return {"reason": "moved", "old": old,
                    "message": "mods are installed, but the Azimuth folder changed (patched: %s)" % old}
        exe = open(os.path.join(self.azimuth, "azimuth.exe"), "rb").read()
        dll_p = os.path.join(self.azimuth, "winmm.dll")
        tls_p = os.path.join(self.azimuth, *TLS.split("/"))
        try:
            idx = open(self.target("hash.manifest"), "rb").read()
            stale = not azimuth.accepts(exe, idx)
            if os.path.isfile(tls_p):
                stale |= azimuth.expected_tls(exe) not in (None, sha256_file(tls_p))
        except azimuth.AzimuthError as e:
            return {"reason": "unreadable", "old": old, "message": str(e)}
        if os.path.isfile(dll_p):
            stale |= open(dll_p, "rb").read().count(azimuth.hashlib.sha256(exe).digest()) != 1
        if fp and self._fh2patch_drifted(fp):
            stale = True
        if stale:
            return {"reason": "updated", "old": old,
                    "message": "the launcher files in %s were replaced (Azimuth / Extra Options update?)" % self.azimuth}
        return None

    def repatch_launcher(self, log=print):
        """bring the Azimuth folder in line with what is installed, without rebuilding the mods (after an Azimuth
        update, or after pointing the settings at a new Azimuth folder)"""
        busy = running()
        if busy:
            raise InstallError("close %s first" % " and ".join(busy))
        if not self.azimuth:
            raise InstallError("set the Azimuth folder first")
        self._launcher_preflight(self.journal["files"])
        changed = self._launcher(log)
        self._save()
        log("launcher %s" % ("re-patched: %s" % self.azimuth if changed else "already up to date"))
        return changed

    def _launcher_preflight(self, files=()):
        """refuse before anything changes if the launcher cannot be patched. files: the game files of the install
        (Mount paths); ones in mediapc / mediaoverride need Extra Options in the Azimuth folder"""
        if not self.azimuth:
            return
        exe_p, dll_p = os.path.join(self.azimuth, "azimuth.exe"), os.path.join(self.azimuth, "winmm.dll")
        if not os.path.isfile(exe_p):
            raise InstallError("no azimuth.exe in %s - check the Azimuth folder in Settings" % self.azimuth)
        if not os.path.isfile(dll_p) and any(_norm(f).startswith(("mediapc/", "mediaoverride/")) for f in files):
            raise InstallError("Extra Options is not in %s (no winmm.dll), and the mods need it - copy Extra "
                               "Options into that folder first. Nothing was changed." % self.azimuth)
        try:
            azimuth.layout(open(exe_p, "rb").read())
        except azimuth.AzimuthError as e:
            raise InstallError("%s. Nothing was changed. Keep using an Azimuth build FH2 Manager knows, or update "
                               "FH2 Manager." % e)
        if os.path.isfile(dll_p):
            dll = open(dll_p, "rb").read()
            if not any(dll.count(d) == 1 for d in self._known_digests()):
                raise InstallError("winmm.dll (Extra Options) in %s was not made for this azimuth.exe or any build "
                                   "FH2 Manager has seen - install the Extra Options release that matches this Azimuth. "
                                   "Nothing was changed." % self.azimuth)

    def _launcher(self, log):
        """bring the Azimuth folder in line with the game folder: release a previous folder, re-install FH2Patch
        where needed, patch azimuth.exe / winmm.dll. True when something changed."""
        if not self.azimuth:
            if not self.fp.matches("hash.manifest", self.target("hash.manifest")):
                log("WARNING: hash.manifest changed but no Azimuth folder is set - Azimuth will refuse the game")
            return False
        self._migrate()
        j = self.journal
        changed = False
        for key in ("fh2patch", "launcher"):
            rec = j.get(key)
            if rec and rec.get("folder") and _norm(rec["folder"]) != _norm(self.azimuth):
                self._release_folder(key, rec, log)
                changed = True
        fp = j.get("fh2patch")
        if fp and (fp.get("folder") is None or self._fh2patch_drifted(fp)):
            self._apply_fh2patch(fp, log)
            changed = True
        return self._patch_launcher(log) or changed

    def _release_folder(self, key, rec, log):
        """the Azimuth folder in the settings changed: put the previous one back (where it still holds our files)"""
        old = rec["folder"]
        if key == "launcher":
            exe_p = os.path.join(old, "azimuth.exe")
            if os.path.isfile(exe_p) and sha256_file(exe_p) == rec["exe_sha256"]:
                for name, kept in (("azimuth.exe", rec["exe_original"]), ("winmm.dll", rec.get("dll_original"))):
                    if kept:
                        self._az_write(old, name, self._kept(kept))
                log("previous Azimuth folder %s restored to its original launcher" % old)
            else:
                log("previous Azimuth folder %s no longer holds the launcher FH2 Manager patched - left as it is" % old)
            self.journal["launcher"] = None
        else:
            self._launcher_files(rec, {}, log, folder=old)
            rec["folder"] = None
            log("FH2Patch taken out of the previous Azimuth folder %s" % old)

    def _fh2patch_drifted(self, rec):
        """FH2Patch's files are not what the tool wrote (e.g. an Azimuth update replaced azimuth-tls.dll)"""
        for rel, r in rec["files"].items():
            p = os.path.join(self.azimuth, *rel.split("/"))
            if not os.path.isfile(p) or sha256_file(p) != r["sha256"]:
                return True
        return False

    def _apply_fh2patch(self, rec, log):
        """(re)install FH2Patch from the kept payload into the current Azimuth folder"""
        if not rec.get("payload"):
            raise InstallError("FH2Patch has to be installed again (its files are not kept from the older version "
                               "of FH2 Manager): run the FH2Patch install")
        binp = os.path.join(self.azimuth, "data", "bin")
        tls_p = os.path.join(binp, "azimuth-tls.dll")
        if not os.path.isfile(tls_p):
            raise InstallError("no data\\bin\\azimuth-tls.dll in %s" % self.azimuth)
        cur = open(tls_p, "rb").read()
        for rel in list(rec["files"]):              # files replaced behind our back: their current bytes are new
            p = os.path.join(self.azimuth, *rel.split("/"))
            if not os.path.isfile(p) or sha256_file(p) != rec["files"][rel]["sha256"]:
                rec["files"].pop(rel)
        tr = rec["files"].get(TLS)
        if tr:
            base = self._kept(tr["original"])
        else:
            if any("fh2patch" in n.lower() for n, _ in peimport.imports(cur)):
                raise InstallError("azimuth-tls.dll was already injected by another tool - restore it first "
                                   "(azimuth-tls.dll.fh2bak)")
            base = cur
        want = {rel: self._kept(s) for rel, s in rec["payload"].items()}
        want[TLS] = peimport.add_import(base, os.path.join(binp, "FH2Patch.dll"), "FH2PatchInit")
        self._launcher_files(rec, want, log)
        rec["folder"] = self.azimuth
        log("FH2Patch set up in %s" % self.azimuth)

    def _patch_launcher(self, log):
        exe_p, dll_p = os.path.join(self.azimuth, "azimuth.exe"), os.path.join(self.azimuth, "winmm.dll")
        tls_p = os.path.join(self.azimuth, "data", "bin", "azimuth-tls.dll")
        if not os.path.isfile(exe_p):
            raise InstallError("no azimuth.exe in %s" % self.azimuth)
        j = self.journal
        idx = open(self.target("hash.manifest"), "rb").read()
        exe = open(exe_p, "rb").read()
        dll = open(dll_p, "rb").read() if os.path.isfile(dll_p) else None
        tls = open(tls_p, "rb").read() if os.path.isfile(tls_p) else None
        rec = j.get("launcher")
        digests = self._known_digests()
        prev = None
        if rec and self._sha(exe) != rec["exe_sha256"]:
            if self._sha(exe) == rec.get("base_sha256"):
                log("azimuth.exe is the original again (restored by hand?) - patching it again")
            else:
                log("azimuth.exe was replaced (Azimuth update?) - the new build is the base now; the previous "
                    "originals stay in the state folder")
                prev, rec = rec, None
                j["launcher"] = None
        if rec and dll is not None and rec.get("dll_sha256") and self._sha(dll) != rec["dll_sha256"] \
                and self._sha(exe) == rec["exe_sha256"]:
            rec["dll_original"] = self._keep_bytes_of(dll_p, "winmm.dll")
            log("winmm.dll was replaced (Extra Options update?) - it is the new original")
        original = self._kept(rec["exe_original"]) if rec else exe
        tls_sha = self._sha(tls) if tls is not None else None
        try:
            original_ok = azimuth.accepts(original, idx) and \
                (tls is None or azimuth.expected_tls(original) in (None, tls_sha))
        except azimuth.AzimuthError:
            original_ok = False
        if rec and original_ok:
            for name, kept in (("azimuth.exe", rec["exe_original"]), ("winmm.dll", rec.get("dll_original"))):
                if kept:
                    self._az_write(self.azimuth, name, self._kept(kept))
            j["launcher"] = None
            log("launcher restored (nothing it checks differs from stock any more)")
            return True
        try:
            need_idx = not azimuth.accepts(exe, idx)
            need_tls = tls is not None and azimuth.expected_tls(exe) not in (None, tls_sha)
            new_exe = azimuth.patched(exe, idx) if need_idx else exe
            if need_tls:
                new_exe = azimuth.with_tls(new_exe, tls)
            new_dll = None
            if dll is not None and dll.count(azimuth.hashlib.sha256(new_exe).digest()) != 1:
                new_dll = azimuth.eo_rehost(dll, exe, new_exe, tuple(self._known_exes()), digests)
        except azimuth.AzimuthError as e:
            raise InstallError("the launcher could not be patched: %s" % e)
        if new_exe == exe and new_dll is None:
            if rec:
                rec.update(folder=self.azimuth, dll_sha256=self._sha(dll) if dll is not None else None)
            return False
        if not rec:
            dll_original = self._keep_bytes_of(dll_p, "winmm.dll") if dll is not None else None
            if prev and dll is not None and prev.get("dll_original") and prev.get("dll_sha256") == self._sha(dll):
                # the update kept the winmm.dll the tool wrote: the original for the new build is the previous
                # original Extra Options, made to accept the new build (what uninstall puts back)
                eo = azimuth.eo_rehost(self._kept(prev["dll_original"]), exe, exe, tuple(self._known_exes()), digests)
                dll_original = self._keep_data(eo, "winmm.dll")
            rec = {"exe_original": self._keep_bytes_of(exe_p, "azimuth.exe"), "dll_original": dll_original,
                   "base_sha256": self._sha(exe)}
        self._az_write(self.azimuth, "azimuth.exe", new_exe)
        if new_dll is not None:
            self._az_write(self.azimuth, "winmm.dll", new_dll)
        ver = azimuth.expected_index(new_exe)[0]
        rec.update(exe_sha256=self._sha(new_exe), folder=self.azimuth, version=ver,
                   dll_sha256=self._sha(new_dll if new_dll is not None else dll) if dll is not None else None)
        j["launcher"] = rec
        what = (["the new index (%d records)" % (len(idx) // 12)] if need_idx else []) + \
               (["the changed azimuth-tls.dll"] if need_tls else [])
        log("launcher patched: Azimuth %s accepts %s%s" % (ver, " and ".join(what) or "the current files",
                                                          ", Extra Options updated" if new_dll is not None else ""))
        return True

    # ------------------------------------------------------------------ FH2Patch (launcher plugin loader)
    def install_fh2patch(self, package, plugins=(), log=print):
        """install FH2Patch (plugin loader) from its release zip / folder, with the named plugins (e.g. FH2_NODIRT).
        The loader is hooked in by giving data\\bin\\azimuth-tls.dll an import of FH2Patch.dll (the same change
        FH2Patch's injector makes; done here without running it); azimuth.exe is updated for that dll. A copy of
        FH2Patch's files is kept, so it follows the launcher to a new folder / build."""
        busy = running()
        if busy:
            raise InstallError("close %s first" % " and ".join(busy))
        if not self.azimuth:
            raise InstallError("set the Azimuth folder first")
        self._launcher_preflight()
        pkg = read_fh2patch(package)
        missing = [p for p in plugins if p.lower() not in {k.lower() for k in pkg["plugins"]}]
        if missing:
            raise InstallError("not in this FH2Patch package: %s (it has: %s)" % (", ".join(missing),
                                                                                ", ".join(sorted(pkg["plugins"]))))
        self._migrate()
        j = self.journal
        if j.get("fh2patch") and j["fh2patch"].get("folder") and _norm(j["fh2patch"]["folder"]) != _norm(self.azimuth):
            self._release_folder("fh2patch", j["fh2patch"], log)
        rec = j.get("fh2patch") or {"files": {}, "plugins": []}
        payload = {"data/bin/FH2Patch.dll": pkg["host"]}
        if pkg["ini"] is not None:
            payload["data/bin/FH2Patch.ini"] = pkg["ini"]
        by_low = {k.lower(): k for k in pkg["plugins"]}
        for p in plugins:
            for fname, data in pkg["plugins"][by_low[p.lower()]].items():
                payload["data/bin/fh2plugins/" + fname] = data
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        rec["payload"] = {}
        for rel, data in payload.items():
            dst = os.path.join(self.state, "fh2patch", "payload", stamp, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").write(data)
            rec["payload"][rel] = "fh2patch/payload/%s/%s" % (stamp, rel)
        rec["plugins"] = [by_low[p.lower()] for p in plugins]
        j["fh2patch"] = rec
        self._apply_fh2patch(rec, log)
        self._save()
        self._patch_launcher(log)
        self._save()
        log("FH2Patch installed%s" % (" with " + ", ".join(rec["plugins"]) if rec["plugins"] else " (no plugins)"))

    def remove_fh2patch(self, log=print):
        busy = running()
        if busy:
            raise InstallError("close %s first" % " and ".join(busy))
        self._migrate()
        rec = self.journal.get("fh2patch")
        if not rec:
            raise InstallError("FH2Patch was not installed by FH2 Manager")
        if rec.get("folder"):
            self._launcher_files(rec, {}, log, folder=rec["folder"])
        self.journal["fh2patch"] = None
        self._save()
        if self.azimuth:
            self._launcher(log)
        self._save()
        log("FH2Patch removed")

    def _launcher_files(self, rec, want, log, folder=None):
        """make the Azimuth folder hold exactly `want` ({path below the Azimuth folder: bytes}) of the files in
        rec["files"]; originals are kept before a file is first replaced and come back when it is no longer wanted.
        A file changed since the tool wrote it is left alone when it is no longer wanted."""
        folder = folder or self.azimuth
        files = rec["files"]
        for rel, data in want.items():
            dst = os.path.join(folder, *rel.split("/"))
            r = files.get(rel)
            if r and os.path.exists(dst) and sha256_file(dst) != r["sha256"]:
                raise InstallError("%s changed after FH2 Manager installed it" % rel)
            if r is None:
                r = {"original": self._keep_bytes_of(dst, rel) if os.path.exists(dst) else None}
            self._az_write(folder, rel, data)
            r["sha256"] = self._sha(data)
            files[rel] = r
        for rel in [r for r in files if r not in want]:
            dst = os.path.join(folder, *rel.split("/"))
            r = files.pop(rel)
            if os.path.exists(dst) and sha256_file(dst) != r["sha256"]:
                log("left %s as it is (changed after FH2 Manager installed it)" % dst)
                continue
            if r["original"]:
                self._az_write(folder, rel, self._kept(r["original"]))
            elif os.path.exists(dst):
                os.remove(dst)
                d = os.path.dirname(dst)                 # folders the install created (fh2plugins) go when empty
                while _norm(d).startswith(_norm(os.path.join(folder, "data", "bin")) + "/") and \
                        os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
                    d = os.path.dirname(d)

    # ------------------------------------------------------------------ uninstall
    def uninstall(self, take_over=False, log=print, allow_owned_removal=False):
        """put the game back the way it was before FH2 Manager (adopted mods return to the game folder)"""
        busy = running()
        if busy:
            raise InstallError("close %s first" % " and ".join(busy))
        self._migrate()
        j = self.journal
        problems = [rel for rel, rec in j["files"].items() if os.path.exists(self.target(rel))
                    and sha256_file(self.target(rel)) != rec["sha256"]]
        if j.get("pending"):                            # an interrupted install: its files may be half written
            problems = []
        self._save_risk(j.get("cars", []), allow_owned_removal)
        if problems and not take_over:
            raise InstallError("nothing was changed - these files were changed after FH2 Manager installed them:\n  "
                               + "\n  ".join(problems))
        if j["files"]:
            self.backup_saves(log)
        files = dict(j["files"])
        for rel, rec in j.get("pending_removals", []):  # an interrupted install's files to take out
            files.setdefault(rel, rec)
        for rel, rec in files.items():
            if rec["was"] in ("stock", "foreign", "adopted"):
                self._put(os.path.join(self.state, *rec["backup"].split("/")), rel)
            elif rec["was"] == "adopted-off" and self.fp.has(rel):
                src = self.baseline.mount_path(self.fp.spelled(rel))
                if src:
                    self._put(src, rel)
            else:
                self._remove(rel)
        for rel, rec in j.get("removed_adopted", {}).items():
            self._put(os.path.join(self.state, *rec["backup"].split("/")), rel)
        n = len(j["files"])
        j.update(files={}, mods=[], cars=[], removed_adopted={}, pending=False)
        j.pop("pending_removals", None)
        fp = j.get("fh2patch")
        if fp:                                      # FH2Patch / plugins installed by the tool go too
            if fp.get("folder"):
                self._launcher_files(fp, {}, log, folder=fp["folder"])
            j["fh2patch"] = None
            log("FH2Patch removed")
        rec = j.get("launcher")
        if rec:                                     # the launcher as it was before - if it is still what we wrote
            folder = rec.get("folder") or self.azimuth
            exe_p = os.path.join(folder, "azimuth.exe") if folder else None
            if exe_p and os.path.isfile(exe_p) and sha256_file(exe_p) == rec["exe_sha256"]:
                for name, kept in (("azimuth.exe", rec["exe_original"]), ("winmm.dll", rec.get("dll_original"))):
                    if kept:
                        self._az_write(folder, name, self._kept(kept))
                log("launcher restored")
            else:
                log("launcher not restored: azimuth.exe is not the one FH2 Manager patched any more (updated?) - "
                    "it was left as it is")
            j["launcher"] = None
        self._save()
        log("uninstalled: %d file(s) restored / removed" % n)
        return n


def read_fh2patch(package):
    """{"host": FH2Patch.dll, "ini": FH2Patch.ini or None, "plugins": {name: {file: bytes}}} from an FH2Patch release
    (zip or folder). The host sits in the injector's inner zip, whose password the release names in a folder called
    "PASSWORD_ <password>"; plugins are the */fh2plugins/* files, grouped by the plugin dll's name."""
    import io
    import re
    import zipfile
    files = {}

    def walk_zip(z, prefix, pwds):
        names = z.namelist()
        pw = [m.group(1).strip() for n in names for m in [re.search(r"PASSWORD_\s*([^/]+)/?$", n)] if m]
        for n in names:
            if n.endswith("/"):
                continue
            try:
                data = z.read(n)
            except RuntimeError:                    # encrypted
                data = None
                for p in pw + list(pwds):
                    try:
                        data = z.read(n, pwd=p.encode("latin-1"))
                        break
                    except RuntimeError:
                        continue
                if data is None:
                    continue
            if n.lower().endswith(".zip"):
                walk_zip(zipfile.ZipFile(io.BytesIO(data)), prefix + n + "/", pw + list(pwds))
            else:
                files[prefix + n] = data

    if os.path.isdir(package):
        for root, _, fs in os.walk(package):
            for f in fs:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, package).replace(os.sep, "/")
                if f.lower().endswith(".zip"):
                    walk_zip(zipfile.ZipFile(p), rel + "/", [])
                else:
                    files[rel] = open(p, "rb").read()
    else:
        walk_zip(zipfile.ZipFile(package), "", [])
    host = [k for k in files if k.lower().endswith("/fh2patch.dll") or k.lower() == "fh2patch.dll"]
    if not host:
        raise InstallError("FH2Patch.dll not found in %s" % package)
    ini = [k for k in files if os.path.dirname(k) == os.path.dirname(host[0]) and k.lower().endswith("/fh2patch.ini")]
    plugins = {}
    for k, data in files.items():
        parts = k.split("/")
        if len(parts) > 1 and parts[-2].lower() == "fh2plugins" and k.lower().endswith((".dll", ".ini")):
            stem = os.path.splitext(parts[-1])[0]
            plugins.setdefault(stem, {})[parts[-1]] = data
    plugins = {k: v for k, v in plugins.items() if any(f.lower().endswith(".dll") for f in v)}
    return {"host": files[host[0]], "ini": files[ini[0]] if ini else None, "plugins": plugins}
