# FH2 Manager

A mod manager for **Forza Horizon 2 (Azimuth)** that lets several mods work at the same time.

Most FH2 car mods ship whole copies of shared game files: the game database, the string tables, the sound-path bank
and the zip list. Install two of them and the second overwrites the first, so only one works. FH2 Manager turns each
mod into the *changes* it makes to the stock game and combines the changes of all your mods into one install.

- Check the mods you want, press **APPLY**. Untick one and APPLY again to remove it.
- Every install is rebuilt from the stock game, so mods never pile up on each other.
- Clashes between mods are found before anything changes, and clashing car / part ids are fixed automatically.
- Mods you installed by hand before can be taken over (**Adopt Existing**).
- Your saves are backed up before every install, and the tool asks before removing a car your save owns.
- **UNINSTALL** puts the game and the launcher back the way they were.

## Requirements

- Windows 10 or 11.
- Forza Horizon 2, in a folder you can write to (the folder Azimuth launches; it contains
  `Mount`).
- **Azimuth** (0.9.9 or 0.9.10; newer builds that check files the same way are recognised).
- **Extra Options** (`winmm.dll` in the Azimuth folder). Most mods need it.
- Optional: **FH2Patch** (launcher plugin loader).

## Getting started


1. Download `FH2Manager.exe` from the [Releases](../../releases) page and put it in a folder of its own. It needs no
   installation. Your settings, mod library and log are kept next to it.
2. Close the game and Azimuth.
3. Open FH2 Manager, go to **Settings** and set:
   - **Game folder**: the folder Azimuth launches (it contains `Mount`).
   - **Azimuth folder**: the folder with `azimuth.exe`.
   - **Mod library**: where imported mods are kept (default: `mods` next to the exe).
   - **Unmodified copy** (optional): a clean copy of the game. Only needed if game files were changed by hand
     before; the tool says when.
4. Go to **Mods** and use **Import Zip...** (or **Import Folder...**) for each mod you downloaded. Imported mods are
   ticked.
5. Press **APPLY**. The strip at the bottom of the window shows the progress and the result.
6. Start the game through Azimuth.

If you have already installed mods by hand (dragged files into the game folder), Press **Adopt Existing** before anything else.
It imports every change already in the game folder as one mod, `existing-mods`, so the tool can manage it.

## Using the app

**Mods** lists your library. Each mod shows its cars, a summary, and a **► warnings** dropdown:

- **WARNING** - the mod changes stock content (edits stock database rows, replaces a stock file, removes stock sound
  paths, ...). These are not errors; the mod installs. They tell you what the mod touches besides its own cars, which
  is where mods can affect each other or the base game.
- **NOTE** - information, such as launcher files the mod ships that FH2 Manager does not install.

**Details** shows everything recorded about a mod (cars and ids, files, warnings). **Remove** deletes a mod from the
library; press APPLY afterwards to take it out of the game too. Mods apply top to bottom (lowest to highest priority).

**Console** shows the full log. It is also written to `fh2manager.log` next to the exe - attach it when you report a
problem.

## Keeping your game and saves safe

- Nothing runs while the game or Azimuth is open.
- The whole install is planned and checked before any file changes. If a file was changed behind the tool's back, or
  belongs to a mod it does not know, nothing is changed and it says which file.
- Stock files are kept before they are replaced (`<game folder>\fh2manager_state\`), and uninstall puts them back.
- **Saves:** before every install that changes the game, your saves are copied to
  `<game folder>\fh2manager_state\save_backups\` (the last 5 are kept). An install that would remove a car your save
  owns stops and asks first - a save whose cars are missing can fail to load. Sell the car in the game first, or keep
  its mod ticked.
- An install that was interrupted (the app was closed or crashed while writing) is reported when the app opens, and the
  next **APPLY** finishes it.

To restore a save backup: close the game, then copy the `saves` folder from a backup over
`<Azimuth folder>\data\titles\<title>\saves`.

## When mods clash

- **Two different cars with the same id, or the same engine / part id for different parts:** car converters keep the
  ids of the game the car came from and only check them against stock FH2, so this happens. When such a mod is
  imported, FH2 Manager gives it free ids (its database rows, the car's model files and thumbnail, its engines and
  their upgrades) and lists them in the import report, e.g. `NEW ID: car 344 -> 2502`. This happens once, at import -
  ids never change after a car is in a garage. For mods imported earlier, the app offers to renumber one of them
  when APPLY stops on such a clash.
- **Identical rows** that two mods both add (for example an engine shared by cars converted from the same game) are
  added once - not a clash.
- Clashes the tool cannot fix (for example two mods replacing the same stock file differently) stop the install with
  a message naming both mods.

## Updating Azimuth

1. Unzip the new Azimuth into its own folder.
2. Copy into it from the old folder: Extra Options (`winmm.dll`, `FH2_ExtraOptions.ini` / `.dat`) and your save
   (`data\titles`).
3. In FH2 Manager, set the new folder as the **Azimuth folder** in Settings and press Save. While mods are installed,
   the app asks to **re-patch** the new folder; the old folder gets its original launcher back.

The app also checks the launcher each time it opens and offers to re-patch it when its files were replaced (for
example a new build extracted over the old folder). A build it cannot read, or a `winmm.dll` made for another build,
stops the install before anything changes.

When mods add files, the game's file index (`Mount\hash.manifest`) grows. Azimuth checks that index, and Extra Options
checks `azimuth.exe`, so FH2 Manager updates both when it installs, and restores them on uninstall.

## FH2Patch

FH2Patch (a plugin loader; plugins such as FH2_NODIRT / FH2_AI_HIDE) can be installed from its release zip without
running its injector - FH2 Manager makes the same change itself and keeps it working when Azimuth is updated or moved:

```
python -m fh2manager fh2patch --game <game folder> --azimuth <folder> --package <FH2Patch release zip> --plugin FH2_NODIRT
python -m fh2manager fh2patch --game <game folder> --azimuth <folder> --remove
```

## Troubleshooting

- **Windows Defender / antivirus flags FH2Manager.exe:** single-file Python programs are sometimes flagged by mistake.
  You can build the exe yourself from this source (see below) or run it from source.
- **"these game files were changed by something other than FH2 Manager":** a mod was installed by hand. Use
  **Adopt Existing**, or set an **Unmodified copy** in Settings when the tool asks for one.
- **A save does not load after removing a mod:** reinstall the mod (tick it, APPLY), or restore a save backup.
- **The last APPLY did not finish:** press APPLY again, or UNINSTALL.

## Command line

Everything the app does is also available from the command line:

```
python -m fh2manager import    --stock <unmodified game folder> --mod <mod zip or folder> --out <new folder> [--library <mods folder>]
python -m fh2manager status    --game <game folder> [--azimuth <folder>] [--stock <unmodified copy>]
python -m fh2manager adopt     --game <game folder> --library <mods folder> [--stock ...]
python -m fh2manager install   --game <game folder> --azimuth <folder> --mod <mod> [--mod ...] [--take-over] [--allow-owned-removal]
python -m fh2manager uninstall --game <game folder> --azimuth <folder> [--allow-owned-removal]
python -m fh2manager repatch   --game <game folder> --azimuth <folder>
python -m fh2manager renumber  --stock <unmodified game folder> --mod <library mod> --library <mods folder>
python -m fh2manager build     --stock <unmodified game folder> --mod <mod> [--mod ...] --out <new folder>
```

## Building from source

Needs Python 3.8 or newer.

- Run from source: `pip install customtkinter`, then `python fh2manager_gui.py`.
- Build the exe: `build_exe.cmd` (makes a local `.venv`, installs customtkinter and PyInstaller, writes
  `dist\FH2Manager.exe`).
- Build a release zip: `release.cmd` (builds the exe, checks it, writes `release\FH2Manager-<version>.zip` with the exe,
  this README and the licence).

The tool ships only the size and SHA-256 of every stock game file (`fh2manager/data/stock_fh2_1.0.0.64.json`) - no
game data. How mods are converted, combined and installed, and the file formats involved, are described in
[docs/TECHNICAL.md](docs/TECHNICAL.md).

## Credits and disclaimer

Azimuth, Extra Options and FH2Patch are made by their own authors; FH2 Manager works with them but does not include
them. FH2 Manager is a fan-made tool. It is not affiliated with or endorsed by Microsoft, Xbox Game Studios,
Playground Games or Turn 10 Studios. Forza and Forza Horizon are trademarks of Microsoft. You need your own copy of the
game. Use at your own risk and keep backups.

## License

See [LICENSE](LICENSE).
