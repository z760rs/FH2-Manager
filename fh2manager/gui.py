"""FH2 Manager desktop UI (customtkinter).

Views: Mods (the imported mods, tick to enable), Console (log), Settings (folders).
APPLY installs the ticked mods into the game folder (rebuilt from stock each time), UNINSTALL restores it.
Settings are kept next to the program (fh2manager_settings.json); the enabled mods in <library>/profile.json.
"""
import json
import os
import re
import shutil
import sys
import threading
import traceback
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import __version__, importer
from .builder import condense
from .installer import Game, InstallError, SaveRisk
from .stock import NotStock

APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(APP_DIR, "fh2manager_settings.json")
LOG_FILE = os.path.join(APP_DIR, "fh2manager.log")                   # the Console, kept for bug reports
LOG_MAX = 1000000
OLD_SETTINGS = os.path.join(APP_DIR, "fh2modkit_settings.json")    # the tool's earlier name
DEFAULTS = {"stock": "", "game": "", "launcher": "", "library": os.path.join(APP_DIR, "mods")}

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "icon.ico")   # tools/make_icon.py draws it

BG, PANEL, BORDER = "#121518", "#1a1f24", "#2b343b"
TEAL, TEAL_HOVER, GREY = "#00adb5", "#00818a", "#aaaaaa"


def font(size=12, bold=False):
    return ctk.CTkFont(family="Consolas", size=size, weight="bold" if bold else "normal")


def set_icon(win):
    """the app's icon on a window (title bar and taskbar); customtkinter keeps it once set"""
    try:
        win.iconbitmap(ICON)
    except Exception:                                   # no icon file (or not on Windows): the default stays
        pass


def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.load(open(SETTINGS if os.path.isfile(SETTINGS) or not os.path.isfile(OLD_SETTINGS)
                                else OLD_SETTINGS, encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


class StatusStrip(ctk.CTkFrame):
    """a slim line under the views while a task runs ("Applying ..."), then its result - the Console stays optional.
    Success clears itself after a while; a stop stays until closed."""

    def __init__(self, app, parent):
        super().__init__(parent, fg_color=PANEL, corner_radius=6, border_width=1, border_color=BORDER, height=34)
        self.app, self.running, self._hide_job = app, False, None
        self.grid_columnconfigure(0, weight=1)
        self.text = ctk.CTkLabel(self, text="", font=font(11), anchor="w", justify="left", text_color=GREY,
                                 wraplength=520)
        self.text.grid(row=0, column=0, padx=(12, 8), pady=6, sticky="w")
        self.bar = ctk.CTkProgressBar(self, mode="indeterminate", width=140, height=6, progress_color=TEAL,
                                      fg_color=BORDER)
        self.console_btn = ctk.CTkButton(self, text="Console", width=70, height=22, fg_color=BORDER,
                                         hover_color=TEAL_HOVER, font=font(10), command=self.console)
        self.close_btn = ctk.CTkButton(self, text="x", width=22, height=22, fg_color="transparent",
                                       hover_color=BORDER, text_color=GREY, font=font(11), command=self.hide)

    def start(self, title):
        if self._hide_job:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        self.running = True
        self.configure(border_color=BORDER)
        self.text.configure(text=title, text_color=TEAL)
        self.console_btn.grid_remove()
        self.close_btn.grid_remove()
        self.bar.grid(row=0, column=1, padx=(0, 12))
        self.bar.start()
        self.grid()

    def done(self, ok, text):
        self.running = False
        self.bar.stop()
        self.bar.grid_remove()
        lines = [x for x in str(text).splitlines() if x.strip()]
        short = " ".join(x.strip() for x in lines[:3]) + (" ..." if len(lines) > 3 else "")
        color = "#2ecc71" if ok else "#e74c3c"
        self.text.configure(text=("Done: " if ok else "Stopped: ") + short, text_color=color)
        self.configure(border_color=color)
        self.console_btn.grid(row=0, column=1, padx=(0, 4))
        self.close_btn.grid(row=0, column=2, padx=(0, 8))
        if ok:
            self._hide_job = self.after(10000, self.hide)

    def notice(self, text):
        """a warning that stays until closed (no task running)"""
        self.running = False
        self.bar.grid_remove()
        self.text.configure(text=text, text_color="#e6a23c")
        self.configure(border_color="#e6a23c")
        self.console_btn.grid(row=0, column=1, padx=(0, 4))
        self.close_btn.grid(row=0, column=2, padx=(0, 8))
        self.grid()

    def console(self):
        self.app.show("console")
        self.hide()

    def hide(self):
        if self.running:
            return
        self._hide_job = None
        self.grid_remove()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("FH2 Manager")
        set_icon(self)
        self.geometry("920x540")
        self.minsize(920, 540)
        self.configure(fg_color=BG)
        self.settings = load_settings()
        self.mod_vars, self.mod_paths = [], []
        self.expanded, self.wrap_labels = set(), []    # mods whose warning list is open; labels that wrap
        self.busy = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- sidebar
        side = ctk.CTkFrame(self, width=200, corner_radius=0, fg_color=PANEL, border_width=1, border_color=BORDER)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_rowconfigure(5, weight=1)
        ctk.CTkLabel(side, text="[ FH2 MANAGER ]", font=font(16, True), text_color=TEAL).grid(row=0, column=0, padx=20, pady=(20, 0))
        ctk.CTkLabel(side, text="v%s mod manager" % __version__, font=font(11), text_color=GREY).grid(
            row=1, column=0, padx=20, pady=(0, 15))
        self.nav = {}
        for i, (key, label) in enumerate((("mods", "Mods"), ("console", "Console"), ("settings", "Settings"))):
            b = ctk.CTkButton(side, text=label, font=font(12), command=lambda k=key: self.show(k))
            b.grid(row=2 + i, column=0, padx=15, pady=5, sticky="ew")
            self.nav[key] = b

        stats = ctk.CTkFrame(side, fg_color="transparent")
        stats.grid(row=6, column=0, padx=20, pady=(0, 10), sticky="ew")
        self.stat_mods = ctk.CTkLabel(stats, text="Mods: 0", font=font(11), text_color=GREY, anchor="w")
        self.stat_mods.pack(fill="x", pady=2)
        self.stat_on = ctk.CTkLabel(stats, text="Enabled: 0", font=font(11), text_color="#2ecc71", anchor="w")
        self.stat_on.pack(fill="x", pady=2)
        self.stat_cars = ctk.CTkLabel(stats, text="Cars: 0", font=font(11), text_color=GREY, anchor="w")
        self.stat_cars.pack(fill="x", pady=2)
        self.stat_inst = ctk.CTkLabel(stats, text="Installed: -", font=font(11), text_color=TEAL, anchor="w")
        self.stat_inst.pack(fill="x", pady=2)

        ctk.CTkButton(side, text="APPLY", command=self.apply, fg_color="#1b4d3e", hover_color="#276955",
                      text_color="#2ecc71", font=font(13, True)).grid(row=7, column=0, padx=15, pady=(5, 5), sticky="ew")
        ctk.CTkButton(side, text="UNINSTALL", command=self.uninstall, fg_color="#4d1b1b", hover_color="#692727",
                      text_color="#e74c3c", font=font(13, True)).grid(row=8, column=0, padx=15, pady=(5, 20), sticky="ew")

        # --- views
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        self.views = {k: self._panel(content) for k in ("mods", "console", "settings")}
        self.status = StatusStrip(self, content)
        self.status.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.status.grid_remove()
        self._mods_view(self.views["mods"])
        self._console_view(self.views["console"])
        self._settings_view(self.views["settings"])

        self.show("mods")
        self.log("FH2 Manager %s ready. Library: %s" % (__version__, self.settings["library"]))
        self.refresh()
        self.after(500, self.check_launcher)
        self.after(600, self.check_pending)

    # ---------------------------------------------------------------- layout helpers
    def _panel(self, parent):
        f = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=6, border_width=1, border_color=BORDER)
        f.grid_columnconfigure(0, weight=1)
        return f

    def _small_button(self, parent, text, cmd, width=100):
        return ctk.CTkButton(parent, text=text, width=width, height=24, fg_color=BORDER, hover_color=TEAL_HOVER,
                             text_color="#ffffff", font=font(11), command=cmd)

    def _mods_view(self, v):
        v.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(v, text="[ IMPORTED MODS ]", font=font(14, True), text_color=TEAL).grid(
            row=0, column=0, padx=20, pady=(15, 10), sticky="w")
        bar = ctk.CTkFrame(v, fg_color="transparent")
        bar.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")
        self._small_button(bar, "Select All", lambda: self.set_all(1)).pack(side="left", padx=(0, 10))
        self._small_button(bar, "Deselect All", lambda: self.set_all(0)).pack(side="left", padx=(0, 10))
        self._small_button(bar, "Import Zip...", self.import_zip).pack(side="left", padx=(0, 10))
        self._small_button(bar, "Import Folder...", self.import_folder, 120).pack(side="left", padx=(0, 10))
        self._small_button(bar, "Adopt Existing", self.adopt, 110).pack(side="left", padx=(0, 10))
        self._small_button(bar, "Refresh", self.refresh, 80).pack(side="left")
        self.mod_list = ctk.CTkScrollableFrame(v, fg_color=BG, corner_radius=4)
        self.mod_list.grid(row=2, column=0, padx=20, pady=(0, 15), sticky="nsew")
        self.mod_list.grid_columnconfigure(0, weight=1)
        self.mod_list.grid_columnconfigure(1, weight=0)
        self.mod_list.bind("<Configure>", self._rewrap, add="+")   # keep the frame's own scroll-region update

    def _console_view(self, v):
        v.grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(v, state="disabled", wrap="word", fg_color=BG, text_color="#00efd1", font=font(12))
        self.log_box.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        for tag, color in (("error", "#e74c3c"), ("warning", "#e6a23c"), ("success", "#2ecc71")):
            self.log_box.tag_config(tag, foreground=color)

    def _settings_view(self, v):
        ctk.CTkLabel(v, text="[ SETTINGS ]", font=font(14, True), text_color=TEAL).grid(
            row=0, column=0, columnspan=3, padx=20, pady=(20, 15), sticky="w")
        v.grid_columnconfigure(1, weight=1)
        self.entries = {}
        rows = (("game", "Game folder"), ("launcher", "Azimuth folder"), ("library", "Mod library"),
                ("stock", "Unmodified copy"))
        for i, (key, label) in enumerate(rows):
            ctk.CTkLabel(v, text=label, font=font(12), text_color=GREY, anchor="w", width=130).grid(
                row=1 + i, column=0, padx=(20, 10), pady=8, sticky="w")
            e = ctk.CTkEntry(v, font=font(12), text_color="#ffffff", fg_color=BG, border_color=BORDER)
            e.insert(0, self.settings.get(key, ""))
            e.grid(row=1 + i, column=1, pady=8, sticky="ew")
            self._small_button(v, "Browse", lambda k=key: self.browse(k), 70).grid(row=1 + i, column=2, padx=(10, 20), pady=8)
            self.entries[key] = e
        ctk.CTkLabel(v, text="Game folder: the folder Azimuth launches (it contains Mount). Azimuth folder: where\n"
                             "azimuth.exe is (it is patched when mods add files). Unmodified copy: OPTIONAL - only\n"
                             "needed if game files were changed by hand before; the tool will say when.\n"
                             "New Azimuth build in a new folder: copy Extra Options and data\\titles (your save)\n"
                             "into it, set it here and Save - the tool offers to re-patch it.",
                     font=font(11), text_color=GREY, justify="left", anchor="w").grid(
            row=6, column=0, columnspan=3, padx=20, pady=(10, 10), sticky="w")
        ctk.CTkButton(v, text="Save", width=100, fg_color=TEAL, hover_color=TEAL_HOVER, font=font(12),
                      command=self.save_settings).grid(row=7, column=0, padx=20, pady=10, sticky="w")

    # ---------------------------------------------------------------- behaviour
    def show(self, key):
        for k, v in self.views.items():
            v.grid_forget()
            self.nav[k].configure(fg_color="transparent", text_color=GREY, border_width=1, border_color=BORDER,
                                  hover_color=BORDER)
        self.views[key].grid(row=0, column=0, sticky="nsew")
        self.nav[key].configure(fg_color=TEAL, text_color="#ffffff", border_width=0, hover_color=TEAL_HOVER)
        self.view = key

    def log(self, msg):
        self.after(0, self._log, msg)

    def _log(self, msg):
        self.log_box.configure(state="normal")
        self._log_file(msg)
        tag = next((t for k, t in (("[ERROR]", "error"), ("[WARNING]", "warning"), ("WARNING:", "warning"),
                                   ("[SUCCESS]", "success")) if k in str(msg)), None)
        for line in str(msg).splitlines() or [""]:
            self.log_box.insert("end", "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), line), tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_file(self, msg):
        try:
            if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
                os.replace(LOG_FILE, LOG_FILE + ".1")    # one older log is kept
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for line in str(msg).splitlines() or [""]:
                    f.write("[%s] %s\n" % (stamp, line))
        except OSError:
            pass

    def browse(self, key):
        p = filedialog.askdirectory(title="Choose the folder")
        if p:
            self.entries[key].delete(0, "end")
            self.entries[key].insert(0, os.path.normpath(p))

    def save_settings(self):
        before = (self.settings.get("game"), self.settings.get("launcher"))
        for k, e in self.entries.items():
            self.settings[k] = e.get().strip()
        try:
            json.dump(self.settings, open(SETTINGS, "w", encoding="utf-8"), indent=1)
            self.log("Settings saved.")
        except OSError as e:
            self.log("[ERROR] Could not save settings: %s" % e)
        self.refresh()
        if (self.settings.get("game"), self.settings.get("launcher")) != before:
            self.check_launcher()

    def _quiet_game(self):
        """the Game for the configured folders, or None (no log lines: for background checks)"""
        g = self.settings.get("game")
        try:
            return Game(g, self.settings.get("launcher") or None, self.settings.get("stock") or None) if g else None
        except (InstallError, SystemExit, OSError, ValueError):
            return None

    def check_pending(self):
        """the last install was interrupted (closed / crashed while writing): say how to finish it"""
        g = self._quiet_game()
        if g is not None and g.journal.get("pending"):
            self.log("[WARNING] The last APPLY did not finish. Press APPLY to finish it, or UNINSTALL.")
            self.status.notice("The last APPLY did not finish - press APPLY to finish it (or UNINSTALL).")

    def check_launcher(self):
        """mods installed and the Azimuth folder changed / updated: offer to re-patch it"""
        if self.busy:
            return
        g = self._quiet_game()
        try:
            c = g.launcher_check() if g else None
        except (InstallError, OSError, ValueError) as e:
            c = {"reason": "unreadable", "old": None, "message": str(e)}
        if not c:
            return
        self.log("[WARNING] Launcher: %s" % c["message"])
        if c["reason"] in ("missing", "unreadable"):
            messagebox.showwarning("Azimuth folder", "%s.\n\nCheck the Azimuth folder in Settings. Until it is "
                                   "fixed, Azimuth will refuse the modded game." % c["message"], parent=self)
            return
        if c["reason"] == "moved":
            msg = ("Mods are installed, and the Azimuth folder changed:\n\n  was:  %s\n  now:  %s\n\nBefore "
                   "re-patching, copy into the new folder from the old one:\n  - Extra Options (winmm.dll, "
                   "FH2_ExtraOptions.ini / .dat)\n  - your save (data\\titles)\n\nRe-patch the new folder now? The old "
                   "folder gets its original launcher back." % (c["old"], self.settings.get("launcher")))
        else:
            msg = ("The launcher files in the Azimuth folder were replaced (Azimuth or Extra Options update?), so "
                   "Azimuth will refuse the modded game.\n\nRe-patch it now?")
        if messagebox.askyesno("Re-patch Azimuth", msg, parent=self):
            self.run("Re-patching the Azimuth folder ...", lambda: "The Azimuth folder is patched for the installed "
                     "mods." if g.repatch_launcher(log=self.log) else "The Azimuth folder was already up to date.")
        else:
            self.log("Not re-patched - the next APPLY does it.")

    # library ----------------------------------------------------------------
    def library(self):
        return self.settings.get("library") or DEFAULTS["library"]

    def profile_path(self):
        return os.path.join(self.library(), "profile.json")

    def scan(self):
        lib = self.library()
        mods = []
        if os.path.isdir(lib):
            for d in sorted(os.listdir(lib)):
                if d.endswith((".renumbering", ".before_renumber")):
                    continue                        # a renumber in progress (renumber.py)
                p = os.path.join(lib, d, "mod.json")
                if os.path.isfile(p):
                    try:
                        mods.append((os.path.join(lib, d), json.load(open(p, encoding="utf-8"))))
                    except ValueError:
                        self.log("[WARNING] %s: unreadable mod.json" % d)
        return mods

    def refresh(self):
        for w in self.mod_list.winfo_children():
            w.destroy()
        self.mod_vars, self.mod_paths, self.wrap_labels = [], [], []
        try:
            enabled = set(json.load(open(self.profile_path(), encoding="utf-8")).get("enabled", []))
        except (OSError, ValueError):
            enabled = None
        mods = self.scan()
        if not mods:
            for i, name in enumerate(("Example car mod (placeholder)", "Example camera mod (placeholder)")):
                chk = ctk.CTkCheckBox(self.mod_list, text=name, state="disabled", font=font(12), text_color="#666666",
                                      fg_color=TEAL, border_color=BORDER, corner_radius=0, checkbox_width=12,
                                      checkbox_height=12)
                chk.grid(row=i, column=0, padx=10, pady=4, sticky="w")
            ctk.CTkLabel(self.mod_list, text="No mods imported yet - use Import Zip... / Import Folder...",
                         font=font(11), text_color=GREY).grid(row=3, column=0, padx=10, pady=(10, 4), sticky="w")
        for i, (path, meta) in enumerate(mods):
            var = ctk.IntVar(value=1 if enabled is None or meta["name"] in enabled else 0)
            cars = [c["media_name"] for c in meta.get("provides", {}).get("cars", [])]
            shown = ", ".join(cars[:3]) + (" +%d more" % (len(cars) - 3) if len(cars) > 3 else "")
            label = "%s  v%s" % (meta["name"], meta.get("version", "?"))
            chk = ctk.CTkCheckBox(self.mod_list, text=label, variable=var, command=self.save_profile, font=font(12),
                                  text_color=GREY, fg_color=TEAL, checkmark_color=TEAL, border_color=BORDER,
                                  hover_color=TEAL_HOVER, corner_radius=0, checkbox_width=12, checkbox_height=12)
            chk.grid(row=4 * i, column=0, padx=10, pady=(6, 0), sticky="w")
            buttons = ctk.CTkFrame(self.mod_list, fg_color="transparent")
            buttons.grid(row=4 * i, column=1, rowspan=2, padx=(4, 10), pady=(6, 0), sticky="ne")
            self._small_button(buttons, "Details", lambda p=path, m=meta: self.show_details(p, m), 64).pack(
                side="left", padx=(0, 6))
            self._small_button(buttons, "Remove", lambda p=path, m=meta: self.remove_mod(p, m), 64).pack(side="left")
            if cars:
                self._wrapped(self.mod_list, "%d car(s): %s" % (len(cars), shown), GREY).grid(
                    row=4 * i + 1, column=0, padx=32, sticky="w")
            items = [("WARNING", w) for w in condense(meta.get("warnings", []))]
            if meta.get("launcher_files"):
                items.append(("NOTE", "%d Azimuth-folder file(s) the mod ships are not installed (copy them by hand "
                                      "if you want them): %s" % (len(meta["launcher_files"]), ", ".join(
                                          sorted({x.split("/")[-1] for x in meta["launcher_files"]})))))
            stats = ctk.CTkFrame(self.mod_list, fg_color="transparent")
            stats.grid(row=4 * i + 2, column=0, columnspan=2, padx=(28, 10), pady=(0, 4), sticky="w")
            ctk.CTkLabel(stats, text="%d file(s)  -  %d DB table(s)" % (
                len(meta.get("files", [])), len(meta.get("gamedb", {}).get("tables", {}))), font=font(10),
                text_color="#666666").pack(side="left", padx=(4, 12))
            if items:
                self._warning_dropdown(stats, 4 * i + 3, meta["name"], items)
            self.mod_vars.append((meta, var))
            self.mod_paths.append(path)
        self.update_stats()
        self.update_installed()

    def _wrapped(self, parent, text, color, size=10):
        """a label that wraps to the width of the mod list"""
        lb = ctk.CTkLabel(parent, text=text, font=font(size), text_color=color, anchor="w", justify="left",
                          wraplength=self._wrap_width())
        self.wrap_labels.append(lb)
        return lb

    def _wrap_width(self):
        return max(300, self.mod_list.winfo_width() - 140) if self.mod_list.winfo_width() > 1 else 600

    def _rewrap(self, _event=None):
        w = self._wrap_width()
        for lb in self.wrap_labels:
            try:
                lb.configure(wraplength=w)
            except Exception:                           # destroyed by a refresh
                pass

    def _warning_dropdown(self, stats, row, name, items):
        """a "► 3 warning(s)" toggle; open, the items are listed under the mod, each marked WARNING or NOTE"""
        n_warn = sum(1 for k, _ in items if k == "WARNING")
        n_note = len(items) - n_warn
        title = "  ".join(x for x in ("%d warning(s)" % n_warn if n_warn else "",
                                      "%d note(s)" % n_note if n_note else "") if x)
        box = ctk.CTkFrame(self.mod_list, fg_color=PANEL, corner_radius=4)
        for kind, text in items:
            line = ctk.CTkFrame(box, fg_color="transparent")
            line.pack(fill="x", padx=8, pady=2)
            ctk.CTkLabel(line, text=kind, width=70, height=16, anchor="nw", font=font(10, True),
                         text_color="#e6a23c" if kind == "WARNING" else TEAL).pack(side="left", anchor="n")
            lb = self._wrapped(line, text, "#cfcfcf")
            lb.configure(height=16, anchor="nw")
            lb.pack(side="left", fill="x", expand=True, anchor="n")
        color = "#e6a23c" if n_warn else TEAL

        def toggle():
            if name in self.expanded:
                self.expanded.discard(name)
                box.grid_remove()
                btn.configure(text="► " + title)
            else:
                self.expanded.add(name)
                box.grid(row=row, column=0, columnspan=2, padx=(32, 10), pady=(0, 8), sticky="ew")
                btn.configure(text="▼ " + title)
                self.after(10, self._rewrap)
        btn = ctk.CTkButton(stats, text="► " + title, width=10, height=20, fg_color="transparent",
                            hover_color=BORDER, text_color=color, font=font(10, True), command=toggle)
        btn.pack(side="left")
        if name in self.expanded:                       # stays open across refreshes
            self.expanded.discard(name)
            toggle()

    def show_details(self, path, meta):
        """everything the import recorded about a mod, in a window that wraps and scrolls"""
        w = ctk.CTkToplevel(self)
        w.title(meta["name"])
        w.after(250, set_icon, w)                       # after customtkinter's own default icon for new windows
        w.geometry("760x480")
        w.configure(fg_color=BG)
        w.transient(self)
        box = ctk.CTkTextbox(w, wrap="word", fg_color=PANEL, text_color="#dddddd", font=font(12))
        box.pack(fill="both", expand=True, padx=10, pady=10)
        lines = ["%s  v%s" % (meta["name"], meta.get("version", "?")),
                 "source: %s" % (meta.get("source") or {}).get("file", "?"), ""]
        cars = meta.get("provides", {}).get("cars", [])
        if cars:
            lines.append("CARS (%d)" % len(cars))
            lines += ["  %s  (id %s)" % (c["media_name"], c["id"]) for c in cars] + [""]
        for old, new in meta.get("renumbered", {}).get("cars", {}).items():
            lines.append("NEW ID: car %s -> %s (another library mod uses %s)" % (old, new, old))
        lines.append("FILES (%d)" % len(meta.get("files", [])))
        lines += ["  %s %s" % (f.get("op", ""), f["path"]) for f in meta.get("files", [])]
        if meta.get("entries"):
            lines.append("  + %d entries in stock zips" % len(meta["entries"]))
        tables = meta.get("gamedb", {}).get("tables", {})
        if tables:
            lines += ["", "DATABASE: %d table(s) changed" % len(tables)]
        warn = condense(meta.get("warnings", []))
        if warn:
            lines += ["", "WARNINGS (%d) - changes to stock content, not errors" % len(warn)] +                 ["  WARNING: " + x for x in warn]
        if meta.get("launcher_files"):
            lines += ["", "NOTE: AZIMUTH-FOLDER FILES - shipped by the mod, NOT installed (copy them by hand if you "
                          "want them)"] + ["  " + x.split("/")[-1] + "    (" + x + ")" for x in meta["launcher_files"]]
        box.insert("end", "\n".join(lines))
        box.configure(state="disabled")
        w.after(100, w.lift)

    def remove_mod(self, path, meta):
        """delete a mod from the library (the game keeps it until the next APPLY)"""
        if self.busy:
            self.log("[WARNING] Busy - wait for the current task to finish.")
            return
        lib = os.path.abspath(self.library())
        if os.path.dirname(os.path.abspath(path)) != lib or not os.path.isfile(os.path.join(path, "mod.json")):
            self.log("[ERROR] %s is not a mod in the library." % path)
            return
        g = self.settings.get("game")
        installed = False
        try:
            installed = bool(g) and meta["name"] in [m["name"] for m in Game(g).journal["mods"]]
        except (InstallError, SystemExit, OSError, ValueError):
            pass
        msg = "Delete '%s' from the mod library?" % meta["name"]
        if installed:
            msg += "\n\nIt is installed in the game: press APPLY afterwards to take it out of the game too."
        if not messagebox.askyesno("Remove mod", msg, parent=self):
            return
        try:
            shutil.rmtree(path)
            self.log("Removed %s from the library." % meta["name"] + (" APPLY to take it out of the game." if installed else ""))
        except OSError as e:
            self.log("[ERROR] Could not remove %s: %s" % (meta["name"], e))
        self.refresh()
        self.save_profile()

    def save_profile(self):
        self.update_stats()
        lib = self.library()
        if not os.path.isdir(lib):
            return
        try:
            json.dump({"enabled": [m["name"] for m, v in self.mod_vars if v.get()]},
                      open(self.profile_path(), "w", encoding="utf-8"), indent=1)
        except OSError as e:
            self.log("[ERROR] Could not save the profile: %s" % e)

    def set_all(self, value):
        for _, v in self.mod_vars:
            v.set(value)
        self.save_profile()

    def update_installed(self):
        g = self.settings.get("game")
        try:
            j = Game(g).journal if g else None
        except (InstallError, SystemExit, OSError, ValueError):
            j = None
        self.stat_inst.configure(text="Installed: %s" % ("-" if j is None else len(j["mods"])))

    def update_stats(self):
        on = [m for m, v in self.mod_vars if v.get()]
        self.stat_mods.configure(text="Mods: %d" % len(self.mod_vars))
        self.stat_on.configure(text="Enabled: %d" % len(on))
        self.stat_cars.configure(text="Cars: %d" % sum(len(m.get("provides", {}).get("cars", [])) for m in on))

    # import ------------------------------------------------------------------
    def import_zip(self):
        p = filedialog.askopenfilename(title="Choose a mod zip", filetypes=[("Zip archives", "*.zip"), ("All", "*.*")])
        if p:
            self.start_import(p)

    def import_folder(self):
        p = filedialog.askdirectory(title="Choose a mod folder (or a modded game folder)")
        if p:
            self.start_import(p)

    def game(self):
        """the Game for the configured folders (None and a log line when the game folder is not set / wrong)"""
        g = self.settings.get("game", "")
        if not g:
            self.log("[ERROR] Set the game folder in Settings first.")
            self.show("settings")
            return None
        try:
            return Game(g, self.settings.get("launcher") or None, self.settings.get("stock") or None)
        except (InstallError, SystemExit) as e:
            self.log("[ERROR] %s" % e)
            self.show("settings")
            return None

    def stock_source(self):
        """what imports compare against: the game folder (checked against the stock fingerprints), else the copy"""
        if self.settings.get("game"):
            g = self.game()
            return g.baseline if g else None
        if self.settings.get("stock"):
            return self.settings["stock"]
        self.log("[ERROR] Set the game folder in Settings first.")
        self.show("settings")
        return None

    def run(self, title, work, failed=None):
        """run work() in a thread; log its errors; refresh afterwards. On the Console the log shows the progress;
        elsewhere a popup does (work() may return a short success message for it)"""
        if self.busy:
            self.log("[WARNING] Busy - wait for the current task to finish.")
            return
        self.busy = True
        popup = None if getattr(self, "view", None) == "console" else self.status
        if popup is not None:
            popup.start(title)
        self.log(title)

        def finish(ok, text):
            if popup is not None:
                self.after(0, popup.done, ok, text)

        def body():
            try:
                res = work()
                finish(True, res if isinstance(res, str) else "Done.")
            except InstallError as e:
                self.log("[ERROR] %s" % e)
                finish(False, str(e))
                if failed:
                    self.after(0, failed, e)
            except NotStock as e:
                self.log("[ERROR] These game files were changed by an earlier install, so the tool has no original "
                         "to compare against:")
                for p in e.paths:
                    self.log("          %s" % p)
                self.log("        Set 'Unmodified copy' in Settings to a clean copy of the game (it is only read), "
                         "or restore these files.")
                finish(False, "Some game files were changed by an earlier install and have no original to compare "
                              "against - see the Console. Set 'Unmodified copy' in Settings.")
                self.after(0, self.show, "settings")
            except BaseException as e:                 # SystemExit included (input errors)
                self.log("[ERROR] %s" % e)
                finish(False, str(e))
                if not isinstance(e, SystemExit):
                    self.log(traceback.format_exc())
            finally:
                self.busy = False
                self.after(0, self.refresh)

        threading.Thread(target=body, daemon=True).start()

    def start_import(self, src):
        stock = self.stock_source()
        if stock is None:
            return
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(os.path.normpath(src)))[0]) or "mod"
        out = os.path.join(self.library(), name)
        if os.path.exists(out):
            self.log("[ERROR] The library already has a mod called %s." % name)
            return

        ticked = [m["name"] for m, v in self.mod_vars if v.get()]

        def work():
            from .__main__ import summary
            os.makedirs(self.library(), exist_ok=True)
            r = importer.import_mod(stock, src, out, name, library=[p for p, _ in self.scan()], log=self.log)
            self.log(summary(r, out))
            self._enable(ticked, r["name"])
            self.log("[SUCCESS] Imported %s - ticked; press APPLY to install it." % name)
            moved = r.get("renumbered", {}).get("cars", {})
            return "Imported %s (ticked).%s\n\nPress APPLY to install it." % (name, "".join(
                "\nCar id %s -> %s (another mod uses %s)." % (a, b, a) for a, b in moved.items()))

        self.run("Importing %s ..." % src, work)

    def _enable(self, ticked, name):
        """the profile with name ticked too (a newly imported mod is meant to be installed)"""
        try:
            json.dump({"enabled": list(dict.fromkeys(ticked + [name]))},
                      open(self.profile_path(), "w", encoding="utf-8"), indent=1)
        except OSError as e:
            self.log("[ERROR] Could not save the profile: %s" % e)

    def adopt(self):
        g = self.game()
        if g is None:
            return
        if not messagebox.askyesno("Adopt existing mods",
                                   "Import every change already in the game folder (mods installed by hand) as one "
                                   "mod called 'existing-mods'?\n\nThe files are backed up; afterwards the tool "
                                   "manages them like any other mod.", parent=self):
            return

        ticked = [m["name"] for m, v in self.mod_vars if v.get()]

        def work():
            os.makedirs(self.library(), exist_ok=True)
            g.adopt(self.library(), log=self.log)
            self._enable(ticked, "existing-mods")
            self.log("[SUCCESS] Existing mods adopted - keep them ticked and APPLY to keep them in the game.")
            return "Existing mods adopted as 'existing-mods' (ticked).\n\nKeep it ticked and press APPLY to keep them."

        self.run("Adopting the existing mods ...", work)

    def enabled_paths(self):
        return [p for (meta, var), p in zip(self.mod_vars, self.mod_paths) if var.get()]

    def _owned_ok(self, e):
        """ask before removing cars the save owns (the saves are backed up first either way)"""
        return messagebox.askyesno(
            "Cars in your save", "Your save looks like it owns these cars, which this would remove:\n\n  %s\n\n"
            "A save whose cars are missing can fail to load. Sell them in the game first, or keep their mod "
            "ticked.\n\nRemove them anyway? (Your saves are backed up before anything changes.)"
            % "\n  ".join("%s (id %s)" % (c["media_name"], c["id"]) for c in e.cars), parent=self, icon="warning")

    def apply(self, take_over=False, allow_owned=False):
        g = self.game()
        if g is None:
            return
        paths = self.enabled_paths()
        names = [os.path.basename(p) for p in paths]

        def work():
            r = g.apply(paths, take_over=take_over, log=self.log, allow_owned_removal=allow_owned)
            warned = set(condense(["%s: %s" % (meta["name"], w) for meta, v in self.mod_vars if v.get()
                                   for w in meta.get("warnings", [])]))
            for n in r["notes"]:                        # the mods' own warnings are marked as such
                self.log(("[WARNING] " if n in warned else "note: ") + n)
            self.log("[SUCCESS] Installed: %s" % (" + ".join(names) or "no mods (game restored to stock)"))
            n_warn = sum(1 for n in r["notes"] if n in warned)
            if not names:
                return "No mods installed - the game is back to stock."
            return "Installed %d mod(s): %s%s" % (len(names), ", ".join(names),
                                                  "  (%d mod warning(s))" % n_warn if n_warn else "")

        def failed(e):
            if isinstance(e, SaveRisk):
                if self._owned_ok(e):
                    self.apply(take_over=take_over, allow_owned=True)
                return
            if "belongs to another mod" in str(e) and messagebox.askyesno(
                    "Files of other mods", "Some files in the game folder come from mods the tool did not install "
                    "(see Console).\n\nBack them up and replace them anyway? (Adopt Existing is the cleaner way.)",
                    parent=self):
                self.apply(take_over=True)
                return
            m = re.search(r"used by two different cars: .+? in .+? and .+? in (.+?) - ", str(e)) or \
                re.search(r"is added by both .+? and (.+?), with different contents", str(e))
            if m:
                self.offer_renumber(m.group(1), g)

        self.run("Applying %s ..." % (" + ".join(names) or "no mods"), work, failed)

    def offer_renumber(self, mod_name, g):
        """two library mods use one id for different things: give the later one new ids"""
        path = next((p for p, meta in self.scan() if meta["name"] == mod_name), None)
        if path is None:
            return
        installed = mod_name in [m["name"] for m in g.journal["mods"]]
        msg = ("'%s' uses a car or part id that another mod in the library uses for something else.\n\n"
               "Give '%s' new ids? Its cars and parts keep working; only their numbers change." % (mod_name, mod_name))
        if installed:
            msg += ("\n\nIt is installed now. If your save owns one of its cars, that car can be lost or break the "
                    "save - sell it first.")
        if not messagebox.askyesno("Renumber mod", msg, parent=self, icon="warning" if installed else "question"):
            return
        stock = self.stock_source()
        if stock is None:
            return

        def work():
            from . import renumber
            others = [p for p, _ in self.scan() if p != path]
            moved = renumber.resolve(path, stock, others, log=self.log)
            text = ("%s was given new ids - press APPLY again." % mod_name if moved["cars"] or moved["parts"]
                    else "Nothing to renumber in %s." % mod_name)
            self.log("[SUCCESS] " + text if moved["cars"] or moved["parts"] else text)
            return text

        self.run("Renumbering %s ..." % mod_name, work)

    def uninstall(self):
        g = self.game()
        if g is None:
            return
        if not messagebox.askyesno("Uninstall", "Restore the game folder and the launcher to how they were before "
                                   "FH2 Manager?", parent=self):
            return
        self._uninstall(g, False)

    def _uninstall(self, g, allow_owned):
        def work():
            g.uninstall(log=self.log, allow_owned_removal=allow_owned)
            return "The game folder and the launcher are back to how they were before FH2 Manager."

        def failed(e):
            if isinstance(e, SaveRisk) and self._owned_ok(e):
                self._uninstall(g, True)
        self.run("Uninstalling ...", work, failed)


def main():
    if os.name == "nt":                                 # its own taskbar entry and icon (not Python's)
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FH2Manager")
        except Exception:
            pass
    app = App()
    if "--selftest" in sys.argv:                       # build the window, close it after a moment
        app.after(1500, app.destroy)
    app.mainloop()


if __name__ == "__main__":
    main()
