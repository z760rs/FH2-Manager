# FH2 Manager - technical notes

For mod authors and developers: how mods are converted, combined and installed, and the file formats involved.

## Change-based mods

FH2 Manager never stores a mod as the files it ships. It imports it as the **changes** it makes to the stock game
(rows added to the database, strings added, sound paths added, new files). Changes from several mods can then be
combined into one install, and every install starts again from the stock files.

### Import a mod

```
python -m fh2manager import --stock <unmodified game folder> --mod <mod zip or folder> --out <new folder> [--library <mods folder>]
```

- `--stock`: the unmodified game - its `Mount` folder, or the folder that contains `Mount`.
- `--mod`: a mod as downloaded (a zip or folder with `Mount/media/...` and/or `Mount/mediapc/...`, also inside a
  wrapper zip), or a whole modded game folder.
- `--out`: a new, empty folder for the converted mod. Optional: `--name`, `--version`.
- `--library`: the other converted mods; ids this mod uses for something else are given new ones (see Renumbering).

The importer prints a summary and writes `mod.json`. It then **verifies** the result: it re-applies the extracted
changes to the stock files and checks they reproduce the mod's own database rows, string tables and sound paths.

It also **warns** about changes to stock content, which can break other cars or mods, such as a mod that removes a
stock car's sound paths, edits stock database rows or replaces a stock file whole.

When a mod ships a file in more than one place, the copy the game uses is imported: `mediaoverride`, then `mediapc`,
then `media` (Extra Options' priority). The exception is `zipmanifest.xml`, which the game reads from `media` only.

### The change-based mod folder

```
mod.json                      what the mod is and changes (see below)
files/<media path>            files the game does not have (car zips, sound banks), or stock files replaced whole
entries/<zip path>/<entry>    entries added to / replaced in a stock zip
changes/gamedb.json           database rows: per table "insert" rows, "update" {key, set, base}, "delete" keys
                              (tables without a primary key: "insert" / "remove" whole rows); table renames, new /
                              dropped tables, views / triggers / indexes
changes/strings.json          per language and table: "add" / "change" {tag, base, new} / "remove" string entries
changes/fmod_strings.json     MasterBank.strings.bank: "add" {path: GUID}, "change", "remove"
changes/zipmanifest.json      zips the mod lists in zipmanifest.xml (path, version, priority)
changes/hash_index.json       Mount\hash.manifest values for the mod's new files (when the mod ships an index)
```

Paths are relative to the game's `media` folder, in lower case. Each changed item keeps the stock value it replaces
(`base`, `base_sha256`), so a builder can tell when two mods change the same thing.

`mod.json` holds the name and version, the source file, the stock database's SHA-256, every file and zip entry
(with SHA-256 hashes), a per-table summary, the cars and makes the mod adds (`provides`), the warnings, ids given by
renumbering (`renumbered`), and `placed`: whether the mod put each file in `media`, `mediapc` or `mediaoverride`.

## Building one install from several mods

```
python -m fh2manager build --stock <unmodified game folder> --mod <mod> --mod <mod> ... --out <new folder>
```

Each `--mod` is a converted mod folder or anything `import` accepts. Mods apply in the order given. The build starts
from the stock files every time, so a mod is never applied twice.

It stops with a report on any **conflict**:

- two mods add the same car (same name): it is the same car twice;
- two different cars use the same car id;
- two mods add the same database key with different contents, or a row, string or sound path is not what a mod
  expects;
- the same file or zip entry has different contents;
- two sound paths share a GUID.

A row or string that two mods add **identically** is added once. Tables nothing refers to by id (`Combo_Colors`) get
the next free id when an id is taken.

Output: `<out>/Mount/...` (the files to place in the game's `Mount` folder) and `<out>/build.json`, which lists every
file with its SHA-256, the stock file it replaces and the mods it comes from.

| What | Where |
|---|---|
| combined database | `mediapc/stripped/gamedbRC.slt` |
| changed string tables only | `mediapc/stripped/stringtables/<lang>/<table>.str` (Extra Options overrides single tables) |
| `zipmanifest.xml`, `MasterBank.strings.bank` | `media/...` (the game reads them before Extra Options takes effect) |
| new files (car zips, sound banks, ...) and replaced / patched stock files | `media/...` |
| index records for the new files | `Mount/hash.manifest` |

New files must be in `media` with a `hash.manifest` record. The game asks every car zip for its install-chunk value at
boot, and a file without a record gets 0 (not installed), so the car loads as a placeholder. A record's value comes
from the mod's own `hash.manifest`; failing that, from a stock file of the same kind (same folder, same name prefix),
or from `--index-donor <new file>=<stock file>`.

Zips a mod ships that are not in FH2's native layout are rewritten into it (central directory sorted by name, the
0x1123 data-offset extra field); the build report says which.

## Renumbering

Car converters keep the source game's ids (a car keeps the id it had in the game it came from, and so do its engines)
and only check them against stock FH2. When a mod enters the library and uses an id that a library mod uses for
something else, the new mod gets a free id:

- **A car** (`Data_Car.Id`): the columns that hold the car id (Ordinal, CarId, ModelId, ...), the `car id x 1000 + n`
  ids it defines (car bodies, upgrades, physics, colours), the model id at offset 19 of its `.carbin` and the part ids
  in it, and its thumbnail files when the mod ships them.
- **A part** (engine, drivetrain, motor, torque curve, aero / spring / anti-sway physics, tyre compound, upgrade rows):
  the part, every reference to it, and the ids derived from it (`engine id x 1000 + n` upgrades and torque curves).
- Ids whose references are not known (wheels, makes, ...) are not moved; the import stops and names the clash.

The renumbered mod is checked against the library on a copy of the stock database before it replaces the imported
one. Renumbering happens once: saves store car and part ids, so they must not change once a car is in a garage.

## Installing

The installer keeps its state next to `Mount`, in `<game folder>/fh2manager_state/`:

- `journal.json`: every file it wrote and what to put back, the launcher files it patched, the mods and cars.
- `originals/`: stock files it replaced, checked against the shipped fingerprints before they are kept.
- `backups/`, `adopted/`: other mods' files it took over.
- `launcher/`: the original `azimuth.exe` / `winmm.dll` versions, filed by hash.
- `fh2patch/`: the FH2Patch files it installed, to re-install them in another Azimuth folder.
- `save_backups/`: copies of the saves made before installs (last 5).

The journal is written before any game file changes and marked as pending until the install is complete, together
with the files still to be removed, so an interrupted install can be finished.

### The launcher

Azimuth checks the game's file index: it keeps a table of (pointer to an expected SHA-256 as 64 hex characters,
u64 value) per checked file; for `hash.manifest` the value is the record count. Known builds have known offsets; for
other builds the table is found by its contents (the game exe's stock SHA-256 and size, followed by the
`hash.manifest` entry). Azimuth 0.9.10 also checks `data\bin\azimuth-tls.dll` against a SHA-256 in its resources.
Extra Options' `winmm.dll` stores the 32-byte SHA-256 of `azimuth.exe`, so it is updated after any exe patch.

FH2Patch is hooked in by giving `azimuth-tls.dll` one import of `<Azimuth>\data\bin\FH2Patch.dll` in a new section -
the same change its injector makes (byte for byte).

The launcher record holds the folder, the original exe / dll and the hashes of what was written:

- `azimuth.exe` replaced (an update): the new build becomes the base; earlier originals are never put back over it.
- `winmm.dll` replaced: it becomes the new original and is pointed at the patched exe.
- The Azimuth folder changed: the previous folder gets its originals back if it still holds what was written, the
  new folder is patched, and FH2Patch is re-installed there.
- A build that cannot be read, or a `winmm.dll` for an unknown build: the install stops before anything changes.

## File formats

`fh2manager/formats/` reads and writes the formats involved:

| Module | Format |
|---|---|
| `strtable.py` | LSB2 string tables (`stripped/stringtables/<lang>.zip`, `*.str`). It rebuilds 2086 of the 2170 stock tables byte for byte. The rest (`appManifest`, `List_AddOnTable`, `TitleUpdate2`, ...) use another header and are handled as whole entries. The database refers to a string as `_&<u32>`, u32 = (table prefix << 16) \| tag. |
| `fmodstrings.py` | FMOD Studio strings bank (`MasterBank.strings.bank`): the STDT radix tree of event / bus / bank paths and GUIDs. |
| `hashmanifest.py` | `Mount\hash.manifest`, the game's file index (12-byte records: u64 path key, i32 value). |
| `zipmanifest.py` | `media\zipmanifest.xml` (zip list; central-directory values are recomputed from the zips). |
| `forzazip.py` | FH2's native zip layout. |

`gamedb.py` diffs the SQLite database; `apply.py` applies change sets; `renumber.py` moves clashing ids;
`installer.py` installs into the game folder and patches the launcher (`azimuth.py`, `peimport.py`).
