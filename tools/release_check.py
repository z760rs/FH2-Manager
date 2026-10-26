"""Make and check the release zip: release/FH2Manager-<version>.zip = dist/FH2Manager.exe + README.md + LICENSE.

Stops (exit 1) when:
  - the source or the exe contain anything that identifies the machine it was built on (the user name, the user
    folder, the computer name, this folder's absolute path - found at run time, nothing is hard coded);
  - the exe bundles files of the launcher tools FH2 Manager works with but does not ship (Azimuth, Extra Options,
    FH2Patch);
  - the zip would hold anything but the three files.
"""
import os
import re
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "FH2Manager.exe")
SHIP = {"FH2Manager.exe": EXE, "README.md": os.path.join(ROOT, "README.md"), "LICENSE": os.path.join(ROOT, "LICENSE")}
THIRD_PARTY = re.compile(r"(?i)winmm|extraoptions|extra_options|fh2patch|fh2_nodirt|fh2_ai_hide|azimuth-tls|"
                         r"azimuth\.exe|spgarage")
problems = []


def machine_strings():
    out = set()
    for v in (os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME"), os.environ.get("USERPROFILE"), ROOT,
              os.path.expanduser("~")):
        if v and len(v) >= 3:
            out.add(v.lower())
            out.add(v.lower().replace("\\", "/"))
    return sorted(out)


NEEDLES = machine_strings()


def scan(label, data):
    low = data.lower()
    for n in NEEDLES:
        for enc in ("utf-8", "utf-16-le"):
            if n.encode(enc) in low:
                problems.append("%s contains %r (%s)" % (label, n, enc))


def version():
    m = re.search(r'__version__ = "([^"]+)"', open(os.path.join(ROOT, "fh2manager", "__init__.py")).read())
    return m.group(1)


# 1. the source that goes on GitHub
for d, dirs, files in os.walk(ROOT):
    dirs[:] = [x for x in dirs if x not in (".venv", "dist", "build", "release", "__pycache__", ".git", "mods")
               and not x.startswith("mods_backup")]
    for f in files:
        if f.endswith((".py", ".md", ".cmd", ".json", ".txt")) or f in ("LICENSE", ".gitignore"):
            if f in ("fh2manager_settings.json", "fh2modkit_settings.json"):
                continue
            p = os.path.join(d, f)
            scan(os.path.relpath(p, ROOT), open(p, "rb").read())

# 2. the exe: its own bytes, and every file / module bundled in it
for name, p in SHIP.items():
    if not os.path.isfile(p):
        problems.append("missing: %s" % os.path.relpath(p, ROOT))
if os.path.isfile(EXE):
    scan("FH2Manager.exe", open(EXE, "rb").read())
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    c = CArchiveReader(EXE)
    for name in c.toc:
        if THIRD_PARTY.search(name):
            problems.append("the exe bundles %s (a launcher tool's file)" % name)
        data = c.extract(name)
        if isinstance(data, bytes):
            scan("exe/" + name, data)
            if name.endswith(".pyz"):
                tmp = os.path.join(tempfile.mkdtemp(), "a.pyz")
                open(tmp, "wb").write(data)
                z = ZlibArchiveReader(tmp)
                for mod in z.toc:
                    try:
                        code = z.extract(mod)
                    except Exception:
                        continue
                    fn = (getattr(code, "co_filename", "") or "").lower()
                    if any(n in fn for n in NEEDLES):
                        problems.append("exe module %s was built with the path %s" % (mod, fn))

if problems:
    print("RELEASE STOPPED:")
    for p in problems[:40]:
        print("  - " + p)
    sys.exit(1)

# 3. the zip: exactly the three files
os.makedirs(os.path.join(ROOT, "release"), exist_ok=True)
out = os.path.join(ROOT, "release", "FH2Manager-%s.zip" % version())
with zipfile.ZipFile(out + ".tmp", "w", zipfile.ZIP_DEFLATED) as z:
    for name, p in SHIP.items():
        z.write(p, name)
names = zipfile.ZipFile(out + ".tmp").namelist()
if sorted(names) != sorted(SHIP):
    os.remove(out + ".tmp")
    print("RELEASE STOPPED: the zip holds %s" % names)
    sys.exit(1)
os.replace(out + ".tmp", out)
print("checked: source and exe carry no machine-specific strings, the exe bundles no launcher-tool files")
print("release: %s (%s)" % (os.path.relpath(out, ROOT), ", ".join(names)))
