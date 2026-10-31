"""Tkinter GUI for The Last Caretaker save editor."""
from __future__ import annotations

import functools
import json
import math
from collections import Counter
from contextlib import contextmanager
from fnmatch import fnmatchcase
import os
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

import last_caretaker_save_edit as save_io
import voyage_humans as human_io
import voyage_gvas as gvas
import voyage_map as map_io
from save_map_view import SaveMapView

# How often the UI thread services Tk while a worker thread runs.
PUMP_INTERVAL = 0.03
NO_HUMAN_PROFESSION = "(No profession)"


class _InputGate:
    """Discard mouse and keyboard input while a long operation runs.

    ``tk busy`` blocks the mouse but leaves keyboard focus where it was, so
    focus also moves to an inert widget that swallows every key press (Tab and
    F5 included) until the gate is released.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.shield: tk.Frame | None = None
        self.previous: tk.Misc | None = None

    @staticmethod
    def discard(_event: object = None) -> str:
        return "break"

    def hold(self) -> bool:
        app = self.app
        try:
            app.tk.call("tk", "busy", "hold", app)
        except tk.TclError:
            return False
        try:
            self.previous = app.focus_get()
        except (KeyError, tk.TclError):
            self.previous = None
        try:
            shield = tk.Frame(app, width=1, height=1, takefocus=0)
            shield.bind("<Key>", self.discard)
            shield.place(x=-10, y=-10)
            shield.update_idletasks()  # Tk ignores focus_set on a window that is not mapped yet
            shield.focus_set()
            self.shield = shield
        except tk.TclError:
            self.shield = None
        return True

    def release(self) -> None:
        app = self.app
        try:
            app.tk.call("tk", "busy", "forget", app)
        except tk.TclError:
            pass
        try:
            if self.shield is not None:
                self.shield.destroy()
            if self.previous is not None and self.previous.winfo_exists():
                self.previous.focus_set()
        except tk.TclError:
            pass
        self.shield = self.previous = None


def _hold_input(app) -> _InputGate | None:
    if not isinstance(app, tk.Misc):
        return None
    gate = _InputGate(app)
    return gate if gate.hold() else None


def _busy_stack(app) -> list:
    stack = getattr(app, "_busy_messages", None)
    return stack if isinstance(stack, list) else []


def _is_busy(app) -> bool:
    return bool(_busy_stack(app))


@contextmanager
def _busy_ui(app, message: str):
    """Show a busy state around a long operation.

    The window keeps repainting while it lasts (see ``_run_in_background``) but
    ignores clicks and key presses, so nothing can start a second operation.
    """
    stack = getattr(app, "_busy_messages", None)
    if stack is None:
        stack = []
        app._busy_messages = stack
    stack.append(message)
    busy_var = getattr(app, "busy_var", None)
    progress = getattr(app, "busy_progress", None)
    if busy_var is not None:
        busy_var.set(message)
    if progress is not None:
        if len(stack) == 1:
            progress.pack(side=tk.RIGHT, padx=(8, 4))
            progress.start(12)
        else:
            progress.step(12)
    if len(stack) == 1 and hasattr(app, "configure"):
        app.configure(cursor="watch")
    if len(stack) == 1:
        app._input_gate = _hold_input(app)
    if hasattr(app, "update_idletasks"):
        app.update_idletasks()

    def update(new_message: str) -> None:
        stack[-1] = new_message
        if busy_var is not None:
            busy_var.set(new_message)
        if progress is not None:
            progress.step(12)
        if hasattr(app, "update_idletasks"):
            app.update_idletasks()

    try:
        yield update
    finally:
        stack.pop()
        if stack:
            if busy_var is not None:
                busy_var.set(stack[-1])
        else:
            gate = getattr(app, "_input_gate", None)
            app._input_gate = None
            if isinstance(gate, _InputGate):
                gate.release()
            if progress is not None:
                progress.stop()
                progress.pack_forget()
            if busy_var is not None:
                busy_var.set("")
            if hasattr(app, "configure"):
                app.configure(cursor="")
        if hasattr(app, "update_idletasks"):
            app.update_idletasks()


class _Stage:
    """Latest progress text from a worker thread (a plain attribute, no Tk)."""

    text = ""

    def set(self, text: str) -> None:
        self.text = text


def _pump_ui(app, stage: _Stage, started: float, shown: str) -> str:
    """Service Tk once and refresh the elapsed-time text. Returns the shown text."""
    stack = _busy_stack(app)
    busy_var = getattr(app, "busy_var", None)
    if stack and busy_var is not None:
        text = stack[-1].rstrip(".")
        if stage.text:
            text += f" - {stage.text}"
        elapsed = int(time.monotonic() - started)
        if elapsed:
            text += f" ({elapsed} s)"
        if text != shown:
            busy_var.set(text)
            shown = text
    # Dispatching real events is only safe while the input gate holds them off.
    gated = isinstance(getattr(app, "_input_gate", None), _InputGate)
    pump = getattr(app, "update" if gated else "update_idletasks", None)
    if callable(pump):
        app._pumping = True
        try:
            pump()
        except tk.TclError:
            pass
        finally:
            app._pumping = False
    return shown


def _run_in_background(app, func, *args, report: bool = False, **kwargs):
    """Run ``func`` on a worker thread while this thread keeps Tk responsive.

    A long call made directly on the UI thread leaves Windows without window
    messages for seconds and the window turns "Not Responding". The worker must
    not touch Tk: read widget variables before calling. With ``report`` the
    worker receives a ``progress`` callback whose text appears in the footer.
    The result, or the exception ``func`` raised, is handed back here.
    """
    stage = _Stage()
    if report:
        kwargs["progress"] = stage.set
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            outcome["value"] = func(*args, **kwargs)
        except BaseException as exc:  # re-raised on the calling thread below
            outcome["error"] = exc

    own_gate = None
    if not isinstance(getattr(app, "_input_gate", None), _InputGate):
        own_gate = _hold_input(app)
        app._input_gate = own_gate
    worker = threading.Thread(target=work, name="editor-worker", daemon=True)
    started = time.monotonic()
    shown = ""
    worker.start()
    try:
        while worker.is_alive():
            worker.join(PUMP_INTERVAL)
            shown = _pump_ui(app, stage, started, shown)
    finally:
        if own_gate is not None:
            app._input_gate = None
            own_gate.release()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _not_while_pumping(method):
    """Ignore a command fired by an event dispatched while a worker runs."""

    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        if getattr(self, "_pumping", False) is True:
            return None
        return method(self, *args, **kwargs)

    return guarded


def _unsaved_edits(app) -> int:
    """Queued scalar edits, human professions and raw stat fields."""
    return (len(app.pending) + len(getattr(app, "human_pending", {}))
            + len(getattr(app, "human_attribute_pending", {})))


def _enable_windows_dpi_awareness() -> None:
    """Tell Windows this process handles its own DPI scaling.

    Without this, Windows silently bitmap-scales the whole window on
    high-DPI/fractional-scaling displays. Tk keeps using its default (96 DPI)
    row height while the OS stretches the rendered text, so rows end up too
    short for the (now larger looking) font and the lines run together. This
    must run before the Tk root window is created.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # PROCESS_SYSTEM_DPI_AWARE (1) is enough for Tk, which does not
        # otherwise respond to PER_MONITOR_AWARE DPI change messages.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


SCRIPT_DIR = Path(__file__).resolve().parent


def _settings_path() -> Path:
    if getattr(sys, "frozen", False):
        local_app_data = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return local_app_data / "The Last Caretaker Save Editor" / "editor_settings.json"
    return SCRIPT_DIR / "editor_settings.json"


SETTINGS_PATH = _settings_path()
PROGRESS_FIELDS = (
    ("PlatformUserIndex", "Platform User Index"),
    ("Level", "Caretaker Level"),
    ("exp", "Experience"),
    ("LevelCapExp", "Level Cap Experience"),
    ("SecurityLevel", "Security Level"),
    ("NumPodsSent", "Pods Sent"),
    ("RocketsCollected", "Rockets Collected"),
    ("PodsCollected", "Pods Collected"),
    ("DistanceTravelledInMeters", "Distance Traveled (m)"),
    ("PlatformDistance", "Platform Distance"),
    ("WorldTime", "World Time"),
    ("PlaytimeTime", "Playtime"),
)
# The player's ``Level`` SurvivalAttribute is the energy/power meter, not the
# caretaker XP level. Display it as "Charge" so the Progress rows read clearly.
PLAYER_STAT_LABELS = {"Level": "Charge"}


def _player_stat_label(label: str) -> str:
    """Rewrite a ``"<stat> - <field>"`` label using ``PLAYER_STAT_LABELS``."""
    stat, sep, field = label.partition(" - ")
    if not sep:
        return PLAYER_STAT_LABELS.get(label, label)
    return f"{PLAYER_STAT_LABELS.get(stat, stat)}{sep}{field}"


ITEM_COLUMNS = (
    ("asset", "Asset", 250),
    ("location", "Location", 125),
    ("category", "Category", 90),
    ("count", "Count", 70),
    ("health", "Health", 85),
    ("variation", "Variation", 75),
    ("actor", "Actor", 240),
)
INVENTORY_TYPE_COLORS = {
    "ammo": "#deedff",
    "memory": "#eadfff",
    "weapons": "#f9dddd",
    "food": "#e0f0d8",
    "materials": "#f4e8cf",
    "parts": "#d9eeeb",
    "cables": "#dcf0f4",
    "resources": "#f4e8cf",
    "consumables": "#edf0ce",
    "modules": "#e0e4fa",
    "equipment": "#e3eaf0",
    "paint": "#f5dfed",
    "cartridges": "#e4dcf0",
    "props": "#ede6df",
    "other": "#ffffff",
}
INVENTORY_ROW_FOREGROUND = "#202a33"


def _inventory_type_tag(item: save_io.InventoryItem) -> str:
    category = save_io.repair_category(item.asset, item.category).split(".", 1)[0]
    if category == "cylinders":
        category = "modules"
    return "inventory:" + category


SCALAR_COLUMNS = (
    ("index", "#", 60),
    ("name", "Property", 220),
    ("type", "Type", 105),
    ("value", "Value", 120),
    ("context", "Context", 520),
    ("offset", "Offset", 100),
)
RESOURCE_COLUMNS = (
    ("resource", "Resource", 105),
    ("value", "Stored Amount", 120),
    ("health", "Health (raw)", 110),
    ("damage", "Extra Damage (raw)", 130),
    ("asset", "Inventory Asset", 220),
    ("location", "Location", 125),
    ("actor", "Owner / Actor", 480),
    ("offset", "Offset", 100),
)


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _game_is_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq VoyageSteam-Win64-Shipping.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "VoyageSteam-Win64-Shipping.exe" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.9g}"
    return str(value)


class _HumanStatDialog(simpledialog.Dialog):
    def __init__(
        self, parent, human: human_io.Human, asset: str,
        initial: dict[human_io.AttributeSource, float],
        focus_source: human_io.AttributeSource | None = None,
    ) -> None:
        self.human = human
        self.asset = asset
        self.initial = initial
        self.focus_source = focus_source
        self.fields: dict[human_io.AttributeSource, ttk.Entry] = {}
        self.parsed: dict[human_io.AttributeSource, float] = {}
        super().__init__(parent, "Edit human stat")

    def body(self, master):
        ttk.Label(
            master, text=f"{human_io.asset_label(self.asset)}\nSocial ID: {self.human.social_id}",
            wraplength=620,
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        for row, (source, value) in enumerate(self.initial.items(), 1):
            ttk.Label(master, text=human_io.ATTRIBUTE_SOURCES[source]).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 8), pady=4)
            entry = ttk.Entry(master, width=24)
            entry.insert(0, str(value))
            entry.grid(row=row, column=1, sticky=tk.EW, pady=4)
            self.fields[source] = entry
            saved = self.human.attribute_sets[source].values[self.asset]
            ttk.Label(master, text=f"Saved: {saved}").grid(
                row=row, column=2, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Label(
            master, wraplength=620,
            text="Edit each stored value independently. Raw growth points may differ from "
                 "in-game stats, units and age. OK queues changes; Save Changes writes them.",
        ).grid(row=len(self.fields) + 1, column=0, columnspan=3, sticky=tk.W, pady=(10, 0))
        master.columnconfigure(1, weight=1)
        if self.focus_source is not None and self.focus_source in self.fields:
            return self.fields[self.focus_source]
        return next(iter(self.fields.values()))

    def validate(self) -> bool:
        parsed = {}
        for source, entry in self.fields.items():
            try:
                parsed[source] = human_io.parse_attribute_value(entry.get())
            except ValueError as exc:
                messagebox.showerror("Invalid stat value", f"{human_io.ATTRIBUTE_SOURCES[source]}: {exc}", parent=self)
                entry.focus_set()
                entry.selection_range(0, tk.END)
                return False
        self.parsed = parsed
        return True

    def apply(self) -> None:
        self.result = self.parsed


class _PlayerWarpDialog(simpledialog.Dialog):
    def __init__(self, parent, world: str, destination: human_io.Position3D) -> None:
        self.world = world
        self.destination = destination
        super().__init__(parent, "Player warp destination")

    def body(self, master):
        ttk.Label(master, text=f"World: {self.world}", wraplength=480).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        for row, (label, value) in enumerate(zip(("X (m):", "Y (m):"), self.destination), 1):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 12))
            ttk.Label(master, text=f"{value:,.3f}").grid(row=row, column=1, sticky=tk.W)
        ttk.Label(master, text="Height Z (m):").grid(row=3, column=0, sticky=tk.W, padx=(0, 12))
        self.height = ttk.Entry(master, width=24)
        self.height.insert(0, repr(self.destination[2]))
        self.height.grid(row=3, column=1, sticky=tk.EW, pady=4)
        ttk.Label(
            master, wraplength=480,
            text="Height starts at the saved player's current Z, not the terrain or water surface. "
                 "Choose a known safe height. The map cannot detect terrain, buildings, water "
                 "depth or collision. You could appear underground, underwater or in midair.\n\n"
                 "OK reviews the move; the next confirmation creates a backup and writes the save. "
                 "This moves only the player, not a ship or vehicle.",
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        return self.height

    def validate(self) -> bool:
        try:
            height = float(self.height.get())
            if not math.isfinite(height) or not math.isfinite(height * 100):
                raise ValueError("Height must be finite.")
        except ValueError:
            messagebox.showerror("Invalid height", "Enter a finite height in meters.", parent=self)
            return False
        self.result = (self.destination[0], self.destination[1], height)
        return True


class LastCaretakerEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("The Last Caretaker Save Editor")
        self.geometry("1360x820")
        self.minsize(1040, 680)
        self.settings = _load_settings()
        self.repair_targets = save_io.normalize_repair_targets(self.settings.get("repair_stack_targets"))
        self.repair_asset_targets = save_io.normalize_asset_targets(
            self.settings.get("repair_asset_targets"))
        self.repair_delete_items = save_io.normalize_deletion_items(
            self.settings.get("repair_delete_items"))
        self.document: save_io.SaveDocument | None = None
        self.candidates: list[Path] = []
        self.pending: dict[int, object] = {}
        self.human_pending: dict[human_io.HumanKey, str | None] = {}
        self.human_attribute_pending: dict[human_io.AttributeKey, float] = {}
        self.item_rows: dict[str, save_io.InventoryItem] = {}
        self.scalar_rows: dict[str, save_io.Entry] = {}
        self.resource_rows: dict[str, save_io.Entry] = {}
        self.resource_records: dict[str, save_io.ResourceAmount] = {}
        self.human_rows: dict[str, human_io.Human] = {}
        self._map_document = None
        self.sort_desc: dict[tuple[int, str], bool] = {}
        self.sort_state: dict[int, tuple[str, bool]] = {}
        self.type_search: dict[int, tuple[str, float]] = {}
        self.search_columns: dict[int, str] = {}
        self.context_target: tuple[ttk.Treeview, str, str] | None = None
        self._build_ui()
        self._open_full_screen()
        self._refresh_saves()
        # Registered before the startup load, which can take a minute on a
        # large AutoSave, so the window stays closable meanwhile.
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_startup_save()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # On some Windows DPI/scaling combinations, ttk's default Treeview
        # row height stays fixed while the actual font renders taller,
        # leaving no vertical gap between rows so the text runs together.
        # Compute the row height from the real font metrics instead of
        # trusting the theme's default.
        default_font = tkfont.nametofont("TkDefaultFont")
        row_height = int(default_font.metrics("linespace") * 1.35)
        style.configure("Treeview", rowheight=row_height)
        for name, normal, active, pressed in (
            ("Repair", "#25803b", "#30964a", "#1b602c"),
            ("Combine", "#666666", "#7a7a7a", "#4d4d4d"),
            ("Purge", "#b52b2b", "#cc3838", "#8f2020"),
        ):
            button_style = f"{name}.TButton"
            style.configure(button_style, background=normal, foreground="white")
            style.map(
                button_style,
                background=[("pressed", pressed), ("active", active)],
                foreground=[("disabled", "#cccccc"), ("!disabled", "white")],
            )
        self._build_context_menu()
        self.bind_all("<F5>", lambda _event: self._reload(prompt=True))
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        save_row = ttk.Frame(top)
        save_row.pack(fill=tk.X)
        ttk.Label(save_row, text="Save file on this PC:").pack(side=tk.LEFT)
        self.save_var = tk.StringVar()
        self.save_combo = ttk.Combobox(
            save_row, textvariable=self.save_var, state="readonly"
        )
        self.save_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.save_combo.bind("<<ComboboxSelected>>", self._select_save)
        actions = ttk.Frame(top)
        actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(actions, text="Refresh", command=self._refresh_saves).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="Browse...", command=self._open).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="Save Changes", command=self._save).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Button(actions, text="Reload", command=lambda: self._reload(prompt=True)).pack(side=tk.LEFT, padx=2)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8)
        self.progress_tab = ttk.Frame(self.notebook, padding=8)
        self.inventory_tab = ttk.Frame(self.notebook, padding=8)
        self.humans_tab = ttk.Frame(self.notebook, padding=8)
        self.map_tab = ttk.Frame(self.notebook, padding=8)
        self.scalars_tab = ttk.Frame(self.notebook, padding=8)
        self.resources_tab = ttk.Frame(self.notebook, padding=8)
        self.backups_tab = ttk.Frame(self.notebook, padding=8)
        self.settings_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.progress_tab, text="Progress")
        self.notebook.add(self.inventory_tab, text="Inventory")
        self.notebook.add(self.humans_tab, text="Humans")
        self.notebook.add(self.map_tab, text="Map")
        self.notebook.add(self.resources_tab, text="Container Resources")
        self.notebook.add(self.scalars_tab, text="All Scalars")
        self.notebook.add(self.backups_tab, text="Backups")
        self.notebook.add(self.settings_tab, text="Settings")
        self._build_settings()
        self._build_progress()
        self._build_inventory()
        self._build_humans()
        self.map_view = SaveMapView(
            self.map_tab, reload_map=lambda: self._load_map(force=True),
            matches_filter=self._matches_filter,
            move_selected=self._move_selected_map_object,
            layer_settings=self.settings.get("map_layers", {}),
            on_layers_changed=self._save_preferences,
            warp_player=self._warp_player_on_map,
        )
        self.map_view.pack(fill=tk.BOTH, expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._map_tab_changed)
        self._build_resources()
        self._build_scalars()
        self._build_backups()
        self._bind_settings_scroll(self.settings_content)

        footer = self.status_bar = ttk.Frame(self, padding=(8, 2))
        # Packed ahead of the notebook so a tall tab can never push it off-screen.
        footer.pack(side=tk.BOTTOM, fill=tk.X, before=self.notebook)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(footer, textvariable=self.status_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.pending_var = tk.StringVar(value="No pending edits")
        ttk.Label(footer, textvariable=self.pending_var).pack(side=tk.RIGHT)
        self.busy_var = tk.StringVar()
        ttk.Label(
            footer, textvariable=self.busy_var, foreground="#8a4f00",
            font=("TkDefaultFont", 9, "bold"),
        ).pack(side=tk.RIGHT, padx=(8, 0))
        self.busy_progress = ttk.Progressbar(footer, mode="indeterminate", length=130)

    def _build_settings(self) -> None:
        ttk.Label(
            self.settings_tab,
            text="Preferences are saved when changed. Player locks edit the loaded save instead.",
        ).pack(anchor=tk.W, pady=(0, 8))
        body = ttk.Frame(self.settings_tab)
        body.pack(fill=tk.BOTH, expand=True)
        self.settings_canvas = tk.Canvas(body, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        self.settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.settings_content = ttk.Frame(self.settings_canvas)
        window = self.settings_canvas.create_window(
            (0, 0), window=self.settings_content, anchor=tk.NW,
        )
        self.settings_content.bind("<Configure>", lambda _e: self.settings_canvas.configure(
            scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", lambda e: self.settings_canvas.itemconfigure(
            window, width=e.width))
        for column in (0, 1):
            self.settings_content.columnconfigure(column, weight=1, uniform="settings")

        def section(title: str, row: int, column: int, *, span: int = 1) -> ttk.LabelFrame:
            frame = ttk.LabelFrame(self.settings_content, text=title, padding=10)
            frame.grid(row=row, column=column, columnspan=span, sticky=tk.NSEW, padx=4, pady=4)
            return frame

        def option(parent, attribute: str, key: str, text: str, default: bool = True,
                   command=None) -> None:
            var = tk.BooleanVar(value=bool(self.settings.get(key, default)))
            setattr(self, attribute, var)
            ttk.Checkbutton(
                parent, text=text, variable=var, command=command or self._save_preferences,
            ).pack(anchor=tk.W, pady=2)

        def help_text(parent, text: str) -> None:
            label = ttk.Label(parent, text=text, wraplength=420)
            label.pack(fill=tk.X, pady=(4, 0))
            label.bind("<Configure>", lambda e: label.configure(wraplength=max(1, e.width)))

        opening = section("Opening saves", 0, 0)
        option(opening, "auto_load_latest_var", "auto_load_latest_save", "Auto Load Latest Save")
        help_text(opening, "Load the most recently modified save on this PC at launch.")
        option(opening, "repair_on_open_var", "repair_on_open", "Repair and Top Off On Open", False)
        help_text(
            opening, "Automatically repair and save after opening or manually reloading a save. "
            "Uses the repair options below without the repair confirmation. Internal reloads "
            "after edits do not run it again. Close the game before opening a save.",
        )
        warnings = section("Confirmations", 0, 1)
        for attribute, key, text in (
            ("confirm_save_var", "confirm_save", "Warn before saving"),
            ("confirm_purge_var", "confirm_purge", "Warn before Purge"),
            ("confirm_repair_var", "confirm_repair", "Warn before Repair & Top Off"),
            ("confirm_delete_var", "confirm_delete", "Warn before Delete"),
        ):
            option(warnings, attribute, key, text)
        help_text(warnings, "Errors, game-running checks and unsaved-edit safeguards always remain active.")

        repair = section("Repair & Top Off", 1, 0)
        option(repair, "repair_combine_var", "repair_combine", "Combine duplicate carried stacks")
        option(repair, "refill_storage_var", "refill_storage_tanks", "Refill Storage Tanks")
        option(repair, "refill_rocket_var", "refill_rocket_resources", "Top Off Rocket Resources")
        buttons = ttk.Frame(repair)
        buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="Stack Targets...", command=self._configure_repair_targets).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(buttons, text="Delete Items...", command=self._configure_deletion_items).pack(side=tk.LEFT)
        help_text(repair, "Stack sizes and exact-name deletion rules apply to both manual and on-open repair. "
                  "Combining and configured deletion run only after the first save succeeds.")
        help_text(repair, "Rocket amounts are configured under Stack Targets... > Rocket Resources "
                  "and always override other refill amounts. Defaults: Electricity 60000, Methane 3000, "
                  "Oxygen 12000. Higher amounts can prevent the rocket from functioning. "
                  "When disabled, these three rocket resources are left untouched by repair.")

        inventory = section("Inventory & Purge", 1, 1)
        option(inventory, "show_empty_items", "show_empty_items", "Show zero-count catalog",
               command=self._inventory_preference_changed)
        option(inventory, "purge_include_world", "purge_include_world", "Include environment/world in Purge")

        backups = section("Backups", 2, 0)
        retention = ttk.Frame(backups)
        retention.pack(fill=tk.X)
        ttk.Label(retention, text="Keep backups:").pack(side=tk.LEFT)
        self.keep_var = tk.IntVar(value=max(1, int(self.settings.get("backup_keep", 20))))
        count = ttk.Spinbox(
            retention, from_=1, to=500, textvariable=self.keep_var, width=6,
            command=self._save_preferences,
        )
        count.pack(side=tk.LEFT, padx=6)
        count.bind("<FocusOut>", lambda _e: self._save_preferences())
        count.bind("<Return>", lambda _e: self._save_preferences())
        help_text(backups, "Retention applies to startup and manual backups and operations that create a backup.")

        locks = section("Player locks (loaded save)", 2, 1)
        self.player_lock_vars: dict[str, tk.BooleanVar] = {}
        self.player_lock_buttons: dict[str, ttk.Checkbutton] = {}
        for key, spec in save_io.PLAYER_LOCKS.items():
            var = tk.BooleanVar(value=False)
            button = ttk.Checkbutton(
                locks, text=spec["label"], variable=var, state=tk.DISABLED,
                command=lambda key=key: self._toggle_player_lock(key),
            )
            button.pack(anchor=tk.W, pady=2)
            self.player_lock_vars[key] = var
            self.player_lock_buttons[key] = button
        help_text(locks, "Reflects the current save, not a global preference. Changing a lock confirms, backs up "
                  "and writes the save immediately.")

    def _bind_settings_scroll(self, widget) -> None:
        widget.bind("<MouseWheel>", lambda e: self.settings_canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        for child in widget.winfo_children():
            self._bind_settings_scroll(child)

    def _save_preferences(self) -> bool:
        try:
            saved = self._write_settings()
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid backup count", "Enter a whole number in Settings > Backups > Keep backups.")
            return False
        if not saved:
            messagebox.showerror("Settings failed", "Could not save editor settings.")
            return False
        self.status_var.set("Settings saved")
        return True

    def _inventory_preference_changed(self) -> None:
        self._populate_inventory()
        self._save_preferences()

    def _reset_map(self) -> None:
        self._map_document = None
        self.map_view.reset("Open Map to read saved object positions.")

    def _map_tab_changed(self, _event=None) -> None:
        if self.notebook.select() == str(self.map_tab) and not _is_busy(self):
            self._load_map()

    @_not_while_pumping
    def _load_map(self, *, force: bool = False) -> None:
        if self.document is None:
            self.map_view.reset()
            return
        if self._map_document is self.document and not force:
            return
        failure: Exception | None = None
        with _busy_ui(self, "Reading saved map positions..."):
            try:
                data = _run_in_background(
                    self, map_io.read_map, self.document.raw, self.document.human_data, report=True,
                    named_locations=map_io.NAMED_LOCATIONS_PATH,
                )
            except (gvas.GvasError, ValueError, struct.error) as exc:
                failure = exc
            else:
                self.map_view.set_data(data)
                self._map_document = self.document
        if failure is not None:
            self._map_document = None
            self.map_view.reset(f"Map could not be read: {failure}. Use Refresh map to retry.")
            messagebox.showerror("Could not read map", str(failure))

    @staticmethod
    def _save_map_relocation(
        document: save_io.SaveDocument, point: map_io.MapPoint,
        destination: human_io.Position3D, keep: int,
    ) -> Path | None:
        relocated = map_io.relocate_actor(document.raw, point, destination)
        return save_io.write_raw_changes(document, relocated, max_backups=keep)

    @staticmethod
    def _save_player_warp(
        document: save_io.SaveDocument, player: map_io.MapPoint,
        destination: human_io.Position3D, keep: int,
    ) -> Path | None:
        if _game_is_running():
            raise RuntimeError("Close The Last Caretaker before writing its save.")
        relocated = map_io.warp_player(document.raw, player, destination)
        return save_io.write_raw_changes(document, relocated, max_backups=keep)

    @_not_while_pumping
    def _warp_player_on_map(self, world: str, destination: human_io.Position3D) -> None:
        document = self.document
        data = self.map_view.data
        if document is None or data is None or self._map_document is not document:
            messagebox.showerror("Map is out of date", "Refresh the map before warping the player.")
            return
        if _unsaved_edits(self):
            messagebox.showerror("Unsaved edits", "Save or discard queued edits before warping the player.")
            return
        player = data.player(world)
        if player is None:
            messagebox.showerror("Player position unavailable", "A unique saved player is required in this world.")
            return
        if _run_in_background(self, _game_is_running):
            messagebox.showerror("Game is running", "Close The Last Caretaker before writing its save.")
            return
        try:
            keep = max(1, self.keep_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid backup count", "Enter a whole number in Settings > Backups > Keep backups.")
            return
        try:
            with _busy_ui(self, "Checking the saved player..."):
                _run_in_background(self, map_io.player_warp_offsets, document.raw, player)
                if _run_in_background(self, save_io.save_is_stale, document):
                    raise ValueError("Save changed on disk; reload it and choose the destination again.")
        except (ValueError, tk.TclError, gvas.GvasError, OSError, struct.error) as exc:
            messagebox.showerror("Cannot warp player", str(exc), parent=self)
            return
        dialog = _PlayerWarpDialog(self, world, destination)
        if dialog.result is None:
            self.status_var.set("Player warp canceled; the save was not changed")
            return
        target = dialog.result
        if not messagebox.askyesno(
            "Warp player and save?",
            f"Move the saved player in {world}?\n\n"
            f"From X / Y / Z: {', '.join(f'{value:,.3f}' for value in player.position)} m\n"
            f"To X / Y / Z: {', '.join(f'{value:,.3f}' for value in target)} m\n\n"
            "Terrain, collision and water height are NOT checked. An unsafe position may "
            "leave the player stuck or falling. Only the player moves, not a ship or vehicle.\n\n"
            f"A backup is created before writing (keep {keep}). Restore it if needed. "
            "Load this save in the game to use the new position.",
            parent=self, icon="warning", default="no",
        ):
            self.status_var.set("Player warp canceled; the save was not changed")
            return
        failure: Exception | None = None
        loaded = False
        backup = None
        with _busy_ui(self, "Saving player warp...") as progress:
            try:
                backup = _run_in_background(self, self._save_player_warp, document, player, target, keep)
            except Exception as exc:
                failure = exc
            else:
                progress("Reloading the saved player and map...")
                loaded = self._load(document.path, confirm_discard=False)
        if failure is not None:
            self.status_var.set("Player warp failed; reload the save before retrying")
            messagebox.showerror("Player warp failed", str(failure), parent=self)
        elif not loaded:
            self.status_var.set(
                f"Player warp saved{f' (backup: {backup.name})' if backup else ''}, but reload failed. "
                "Reload this save before editing again.")
        elif backup is None:
            self.status_var.set("Player already at the requested position; no save or backup was needed.")
        else:
            self.status_var.set(
                f"Warped player to {', '.join(f'{value:,.3f}' for value in target)} m "
                f"(backup: {backup.name}). Load this save in the game.")

    @_not_while_pumping
    def _move_selected_map_object(self, point: map_io.MapPoint) -> None:
        document = self.document
        data = self.map_view.data
        if document is None or data is None or self._map_document is not document:
            messagebox.showerror("Map is out of date", "Refresh the map before moving an object.")
            return
        if _unsaved_edits(self):
            messagebox.showerror(
                "Unsaved edits",
                "Save or discard queued edits before moving a map object.",
            )
            return
        if point.move_block_reason:
            messagebox.showerror("Object cannot be moved", point.move_block_reason)
            return
        player = data.player(point.world)
        if player is None:
            messagebox.showerror(
                "Player position unavailable",
                "A unique saved player position is required in the object's world.",
            )
            return
        if _run_in_background(self, lambda: _game_is_running()):
            messagebox.showerror("Game is running", "Close The Last Caretaker before writing its save.")
            return
        try:
            keep = max(1, self.keep_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid backup count", "Enter a whole number in Settings > Backups > Keep backups.")
            return
        destination = (player.position[0], player.position[1] - 1.0, player.position[2])
        prompt = (
            f"Move this saved object?\n\n{point.label}\n{point.actor}\n"
            f"From: {point.position[0]:,.2f}, {point.position[1]:,.2f}, "
            f"{point.position[2]:,.2f} m\n"
            f"To: {destination[0]:,.2f}, {destination[1]:,.2f}, "
            f"{destination[2]:,.2f} m (saved player position, 1 m north)\n\n"
            f"A backup will be created first (keep {keep})."
        )
        if not messagebox.askyesno("Move selected object?", prompt, parent=self):
            return

        failure: Exception | None = None
        loaded = False
        backup: Path | None = None
        with _busy_ui(self, f"Moving {point.label}...") as progress:
            try:
                backup = _run_in_background(
                    self, self._save_map_relocation, document, point, destination, keep,
                )
            except Exception as exc:
                failure = exc
            else:
                progress("Reloading the saved map and object details...")
                loaded = self._load(document.path, confirm_discard=False)
        if failure is not None:
            self.status_var.set("Object move failed; the save was not updated")
            messagebox.showerror("Object move failed", str(failure), parent=self)
            return
        if backup is None:
            self.status_var.set("Object is already at the requested position; no save or backup was needed.")
        elif loaded:
            self.status_var.set(
                f"Moved {point.label} to the saved player position, 1 m north "
                f"(backup: {backup.name})."
            )
        else:
            self.status_var.set(
                f"Object move was saved (backup: {backup.name}), but the editor could not reload it. "
                "Reload this save before editing again."
            )

    def _make_tree(
        self, parent: ttk.Frame, columns: tuple[tuple[str, str, int], ...], *, search_column: str = ""
    ) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="extended")
        ybar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        xbar = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for name, title, width in columns:
            tree.heading(name, text=title, command=lambda t=tree, c=name: self._sort_tree(t, c))
            tree.column(name, width=width, stretch=True)
        tree.bind("<Control-a>", lambda event, t=tree: self._select_all(t))
        tree.bind("<Home>", lambda event, t=tree: self._list_edge(t, first=True, event=event))
        tree.bind("<End>", lambda event, t=tree: self._list_edge(t, first=False, event=event))
        tree.bind("<Control-c>", lambda event, t=tree: self._copy_rows(t))
        tree.bind("<KeyPress>", lambda event, t=tree: self._type_to_jump(t, event))
        tree.bind("<Button-3>", lambda event, t=tree: self._show_context_menu(t, event))
        self.search_columns[id(tree)] = search_column or columns[0][0]
        return tree

    def _build_filters(self, parent: ttk.Frame, prefix: str, apply) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row, text="Filter:").pack(side=tk.LEFT)
        variable = tk.StringVar()
        setattr(self, f"{prefix}_filter", variable)
        setattr(self, f"{prefix}_filter_applied", "")
        entry = ttk.Entry(row, textvariable=variable, width=45)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        entry.bind("<Return>", lambda _event: apply())
        ttk.Button(row, text="Go", width=4, command=apply).pack(side=tk.LEFT)
        ttk.Button(row, text="Clear", width=6, command=lambda: self._clear_filters(prefix, apply)).pack(
            side=tk.LEFT, padx=2,
        )
        return row

    @staticmethod
    def _matches_filter(query: str, value: str) -> bool:
        text = str(value).casefold()
        for term in query.casefold().split():
            exclude = term.startswith("-") and len(term) > 1
            pattern = term[1:] if exclude else term
            matches = fnmatchcase(text, f"*{pattern}*")
            if matches == exclude:
                return False
        return True

    def _apply_filters(self, prefix: str, populate) -> None:
        variable = getattr(self, f"{prefix}_filter")
        setattr(self, f"{prefix}_filter_applied", variable.get())
        populate()

    def _clear_filters(self, prefix: str, apply) -> None:
        getattr(self, f"{prefix}_filter").set("")
        apply()

    def _build_context_menu(self) -> None:
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Copy Cell", command=self._copy_cell)
        self.context_menu.add_command(label="Copy Column (selected rows)", command=self._copy_column)
        self.context_menu.add_command(label="Copy Row(s)", command=lambda: self._copy_rows())
        self.context_menu.add_command(
            label="Copy Row(s) with Headers", command=lambda: self._copy_rows(headers=True)
        )

    def _show_context_menu(self, tree: ttk.Treeview, event: tk.Event) -> str:
        iid = tree.identify_row(event.y)
        column = tree.identify_column(event.x) or "#1"
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        if iid:
            tree.focus(iid)
        tree.focus_set()
        self.context_target = (tree, iid, column)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def _set_clipboard(self, text: str, label: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set(f"Copied {label} to the clipboard")

    def _column_name(self, tree: ttk.Treeview, column: str) -> str:
        try:
            index = int(column.lstrip("#")) - 1
        except ValueError:
            return ""
        columns = list(tree["columns"])
        return columns[index] if 0 <= index < len(columns) else ""

    def _copy_cell(self) -> None:
        if not self.context_target:
            return
        tree, iid, column = self.context_target
        name = self._column_name(tree, column)
        if not iid or not name:
            return
        self._set_clipboard(tree.set(iid, name), "cell")

    def _copy_column(self) -> None:
        if not self.context_target:
            return
        tree, iid, column = self.context_target
        name = self._column_name(tree, column)
        if not name:
            return
        rows = list(tree.selection()) or ([iid] if iid else [])
        if not rows:
            return
        self._set_clipboard("\n".join(tree.set(row, name) for row in rows), f"{len(rows)} cell(s)")

    def _copy_rows(self, tree: ttk.Treeview | None = None, *, headers: bool = False) -> str:
        if tree is None:
            if not self.context_target:
                return "break"
            tree = self.context_target[0]
        rows = list(tree.selection())
        if not rows and self.context_target and self.context_target[1]:
            rows = [self.context_target[1]]
        if not rows:
            return "break"
        columns = list(tree["columns"])
        lines = []
        if headers:
            lines.append("\t".join(tree.heading(name, "text") for name in columns))
        for row in rows:
            lines.append("\t".join(tree.set(row, name) for name in columns))
        self._set_clipboard("\n".join(lines), f"{len(rows)} row(s)")
        return "break"

    def _type_to_jump(self, tree: ttk.Treeview, event: tk.Event) -> str | None:
        char = event.char
        if not char or not char.isprintable() or char.isspace() or event.state & 0x0004:
            return None
        now = time.monotonic()
        prefix, last = self.type_search.get(id(tree), ("", 0.0))
        prefix = (prefix + char) if now - last <= 0.9 else char
        self.type_search[id(tree)] = (prefix, now)
        column = self.search_columns.get(id(tree)) or list(tree["columns"])[0]
        needle = prefix.casefold()
        rows = list(tree.get_children())
        current = tree.focus()
        start = rows.index(current) + 1 if len(prefix) == 1 and current in rows else 0
        order = rows[start:] + rows[:start]
        for iid in order:
            if tree.set(iid, column).casefold().startswith(needle):
                tree.selection_set(iid)
                tree.focus(iid)
                tree.see(iid)
                break
        return "break"

    def _build_progress(self) -> None:
        intro = ttk.Label(
            self.progress_tab,
            text="Core progression fields found in the player profile. Double-click a value to edit, "
                 "or select multiple rows (Shift/Ctrl-click, Ctrl+A) to set them all at once.",
        )
        intro.pack(anchor="w", pady=(0, 6))
        self.charge_status_var = tk.StringVar()
        ttk.Label(
            self.progress_tab,
            textvariable=self.charge_status_var,
            foreground="#8a4f00",
        ).pack(anchor="w", pady=(0, 6))
        columns = (("label", "Field", 270), ("value", "Value", 160), ("type", "Type", 120), ("offset", "Offset", 110))
        self.progress_tree = self._make_tree(self.progress_tab, columns)
        self.progress_tree.bind("<Double-1>", self._edit_progress)
        actions = ttk.Frame(self.progress_tab)
        actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(actions, text="Edit Selected...", command=self._edit_progress).pack(side=tk.LEFT)
        ttk.Button(actions, text="Set Level...", command=lambda: self._quick_edit("Level")).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="Set Experience...", command=lambda: self._quick_edit("exp")).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            actions, text="Boost Health/Energy Enhancers...", command=self._boost_survival_enhancers
        ).pack(side=tk.LEFT, padx=2)

    def _build_inventory(self) -> None:
        toolbar = ttk.Frame(self.inventory_tab)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        self._build_filters(toolbar, "item", self._apply_item_filter)
        filter_row = ttk.Frame(toolbar)
        filter_row.pack(fill=tk.X)
        ttk.Label(filter_row, text="Location:").pack(side=tk.LEFT, padx=(8, 2))
        self.location_filter = tk.StringVar(value="Carried (all)")
        location_combo = ttk.Combobox(
            filter_row,
            textvariable=self.location_filter,
            state="readonly",
            width=17,
            values=("All", "Carried (all)") + save_io.ALL_LOCATIONS,
        )
        location_combo.pack(side=tk.LEFT, padx=(0, 6))
        location_combo.bind("<<ComboboxSelected>>", lambda _event: self._populate_inventory())

        edit_row = ttk.Frame(toolbar)
        edit_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(edit_row, text="Add Item...", command=self._add_item).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_row, text="Delete Selected", command=self._delete_items).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_row, text="Set Count...", command=lambda: self._set_item_field("ItemCount", "count")).pack(side=tk.LEFT, padx=2)

        maintenance_row = ttk.Frame(toolbar)
        maintenance_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            maintenance_row, text="Repair & Top Off", style="Repair.TButton",
            command=self._repair_and_top_off,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            maintenance_row, text="Remove Duplicate Stacks", style="Combine.TButton",
            command=self._remove_duplicate_stacks,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            maintenance_row, text="Purge Empty Slots", style="Purge.TButton",
            command=self._purge_empty_slots,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(maintenance_row, text="Mark Blueprints Scanned", command=self._mark_blueprints_scanned).pack(side=tk.LEFT, padx=2)
        self.inventory_tree = self._make_tree(self.inventory_tab, ITEM_COLUMNS, search_column="asset")
        for category, background in INVENTORY_TYPE_COLORS.items():
            self.inventory_tree.tag_configure(
                f"inventory:{category}", background=background,
                foreground=INVENTORY_ROW_FOREGROUND,
            )
        self.inventory_tree.bind("<Double-1>", self._edit_item_cell)
        self.inventory_tree.bind("<Delete>", self._delete_items_event)
        self.inventory_tree.bind("<KP_Delete>", self._delete_items_event)

    def _build_humans(self) -> None:
        self.human_summary_var = tk.StringVar(value="Load a save to view its humans.")
        summary = ttk.Label(self.humans_tab, textvariable=self.human_summary_var, wraplength=980)
        summary.pack(fill=tk.X, pady=(0, 6))
        summary.bind("<Configure>", lambda event: summary.configure(wraplength=max(1, event.width)))
        self.human_notebook = ttk.Notebook(self.humans_tab)
        self.human_notebook.pack(fill=tk.BOTH, expand=True)
        roster_tab = ttk.Frame(self.human_notebook, padding=8)
        committee_tab = ttk.Frame(self.human_notebook, padding=8)
        self.human_notebook.add(roster_tab, text="Roster")
        self.human_notebook.add(committee_tab, text="Committee roles")

        filters = self._build_filters(roster_tab, "human", self._apply_human_filter)
        ttk.Label(filters, text="Location:").pack(side=tk.LEFT, padx=(10, 0))
        self.human_location_filter = tk.StringVar(value="All")
        locations = ttk.Combobox(
            filters, textvariable=self.human_location_filter, state="readonly",
            values=("All", "In space", "In pod", "Other"), width=12,
        )
        locations.pack(side=tk.LEFT, padx=(4, 0))
        locations.bind("<<ComboboxSelected>>", lambda _event: self._populate_humans())
        actions = ttk.Frame(roster_tab)
        actions.pack(fill=tk.X, pady=(2, 6))
        ttk.Label(actions, text="Profession:").pack(side=tk.LEFT)
        self.human_profession_var = tk.StringVar()
        self.human_profession_combo = ttk.Combobox(
            actions, textvariable=self.human_profession_var, state=tk.DISABLED, width=28,
            values=(NO_HUMAN_PROFESSION,) + tuple(sorted(human_io.PROFESSION_ASSETS)),
        )
        self.human_profession_combo.pack(side=tk.LEFT, padx=4)
        self.human_profession_combo.bind("<<ComboboxSelected>>", self._queue_human_profession)
        self.human_save_button = ttk.Button(
            actions, text="Save Changes", command=self._save, state=tk.DISABLED,
        )
        self.human_save_button.pack(side=tk.LEFT, padx=4)
        self.human_discard_button = ttk.Button(
            actions, text="Discard Human Changes", command=self._discard_human_changes,
            state=tk.DISABLED,
        )
        self.human_discard_button.pack(side=tk.LEFT, padx=4)
        hint = ttk.Label(
            roster_tab, wraplength=980,
            text="Queue professions or raw stats, then Save Changes to write all queued edits.",
        )
        hint.pack(fill=tk.X, pady=(0, 6))
        hint.bind("<Configure>", lambda event: hint.configure(wraplength=max(1, event.width)))

        panes = ttk.Panedwindow(roster_tab, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True)
        roster = ttk.Frame(panes)
        details = ttk.Frame(panes)
        panes.add(roster, weight=3)
        panes.add(details, weight=1)
        self.human_tree = self._make_tree(roster, (
            ("index", "#", 45), ("id", "Social ID", 300),
            ("location", "Location / result", 140), ("pod_coordinates", "Pod X / Y / Z (m)", 205),
            ("pod_distance", "3D distance (m)", 115), ("professions", "Professions", 220),
            ("committees", "Committee job matches", 240), ("traits", "Other traits", 190),
            ("world", "World", 130),
        ), search_column="professions")
        self.human_tree.configure(height=9)
        self.human_tree.bind("<<TreeviewSelect>>", self._show_human_selection)
        self.human_details_var = tk.StringVar(
            value="Select one human to view saved attributes; Ctrl/Shift-click selects several.")
        detail_label = ttk.Label(details, textvariable=self.human_details_var, wraplength=980)
        detail_label.pack(fill=tk.X, pady=(0, 4))
        detail_label.bind("<Configure>", lambda event: detail_label.configure(wraplength=max(1, event.width)))
        stat_actions = ttk.Frame(details)
        stat_actions.pack(fill=tk.X, pady=(0, 4))
        self.human_stat_button = ttk.Button(
            stat_actions, text="Edit Stat...", command=self._edit_human_attribute, state=tk.DISABLED,
        )
        self.human_stat_button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            stat_actions, text="* = unsaved. Raw values may differ from in-game stats, units and age.",
        ).pack(side=tk.LEFT)
        self.human_attribute_tree = self._make_tree(details, (
            ("attribute", "Attribute", 300), ("roster", "Roster (raw)", 170),
            ("current", "Pod current (raw)", 170), ("simulated", "Pod simulated (raw)", 170),
        ))
        self.human_attribute_tree.configure(height=4, selectmode="browse")
        self.human_attribute_tree.bind("<<TreeviewSelect>>", self._update_human_action)
        self.human_attribute_tree.bind("<Double-1>", self._edit_human_attribute)
        self.human_attribute_tree.bind("<Return>", self._edit_human_attribute)

        self._build_filters(committee_tab, "committee", self._apply_committee_filter)
        note = ttk.Label(
            committee_tab, wraplength=980,
            text="Profession coverage from humans in space, including queued unsaved professions; "
                 "not assigned committee members. "
                 "All v6 roles require age 18+; age and operational readiness are not inferred.",
        )
        note.pack(fill=tk.X, pady=(0, 6))
        note.bind("<Configure>", lambda event: note.configure(wraplength=max(1, event.width)))
        self.committee_tree = self._make_tree(committee_tab, (
            ("committee", "Committee", 200), ("coverage", "Roles present", 100),
            ("profession", "Required profession", 210), ("matches", "Humans in space", 120),
            ("status", "Job coverage", 100), ("tier", "Tier", 45),
            ("state", "Saved state", 100), ("world", "World", 130),
            ("ids", "Matching Social IDs (not assignments)", 420),
        ), search_column="committee")

    @_not_while_pumping
    def _apply_human_filter(self) -> None:
        self._apply_filters("human", self._populate_humans)

    @_not_while_pumping
    def _apply_committee_filter(self) -> None:
        self._apply_filters("committee", self._populate_committees)

    def _human_professions(self, human: human_io.Human) -> tuple[str, ...]:
        """Professions as they will be saved, including a queued change."""
        if human.key in self.human_pending:
            profession = self.human_pending[human.key]
            return (profession,) if profession is not None else ()
        return human.professions

    def _human_values(self, human: human_io.Human) -> tuple:
        saved = "; ".join(map(human_io.profession_label, human.professions)) or "No profession"
        professions = self._human_professions(human)
        if human.key in self.human_pending:
            label = "; ".join(map(human_io.profession_label, professions)) or "No profession"
            shown = f"{label} (unsaved; was {saved})"
        else:
            shown = saved
        location = human.location
        if any(key[0] == human.key for key in self.human_attribute_pending):
            location += " (unsaved stats)"
        pod_position = human.pod.position if human.pod is not None else None
        coordinates = (
            ", ".join(f"{coordinate / 100:,.1f}" for coordinate in pod_position)
            if pod_position is not None else "-"
        )
        distance = human.pod_distance_m
        return (
            human.index, human.social_id, location, coordinates,
            f"{distance:,.0f}" if distance is not None else "-", shown,
            "; ".join(human_io.committees_for(professions)) or "-",
            "; ".join(map(human_io.asset_label, human.other_traits)) or "-",
            human.world,
        )

    @_not_while_pumping
    def _populate_humans(self) -> None:
        selection = set(self.human_tree.selection())
        self.human_tree.delete(*self.human_tree.get_children())
        self.human_rows.clear()
        data = self.document.human_data if self.document else human_io.HumanData()
        location = self.human_location_filter.get()
        if not data.error:
            for human in data.humans:
                if location == "Other":
                    if human.location in ("In space", "In pod"):
                        continue
                elif location != "All" and human.location != location:
                    continue
                values = self._human_values(human)
                searchable = " ".join(map(str, values)) + " " + " ".join(human.traits.values)
                if not self._matches_filter(self.human_filter_applied, searchable):
                    continue
                iid = "human:" + json.dumps(human.key)
                self.human_rows[iid] = human
                self.human_tree.insert("", tk.END, iid=iid, values=values)
        self._apply_saved_sort(self.human_tree)
        self.human_tree.selection_set([iid for iid in self.human_rows if iid in selection])
        self._update_human_summary()
        self._show_human_selection()
        self._populate_committees()

    def _update_human_summary(self) -> None:
        data = self.document.human_data if self.document else human_io.HumanData()
        if data.error:
            self.human_summary_var.set(data.error + " Human editing is disabled; other tabs remain available.")
        elif not self.document:
            self.human_summary_var.set("Load a save to view its humans.")
        elif not data.humans:
            self.human_summary_var.set("No grown humans are stored in this save yet.")
        else:
            spaces = sum(h.in_space for h in data.humans)
            pods = sum(h.pod is not None and not h.in_space for h in data.humans)
            blocked = sum(bool(h.edit_error) for h in data.humans)
            text = f"Showing {len(self.human_rows)} of {len(data.humans)} humans | {spaces} in space | {pods} in pods"
            if blocked:
                text += f" | {blocked} read-only: select a human for the reason"
            if self.human_pending or self.human_attribute_pending:
                text += (f" | Queued: {len(self.human_pending)} profession(s), "
                         f"{len(self.human_attribute_pending)} stat(s)")
            self.human_summary_var.set(text)

    @_not_while_pumping
    def _show_human_selection(self, _event=None) -> None:
        selected = [self.human_rows[iid] for iid in self.human_tree.selection() if iid in self.human_rows]
        attributes_selected = self.human_attribute_tree.selection()
        attribute_scroll = self.human_attribute_tree.yview()[0]
        self.human_attribute_tree.delete(*self.human_attribute_tree.get_children())
        common = {self._human_professions(human) for human in selected}
        label = ""
        if len(common) == 1:
            professions = next(iter(common))
            if not professions:
                label = NO_HUMAN_PROFESSION
            elif len(professions) == 1:
                label = human_io.PROFESSIONS.get(professions[0], "")
        self.human_profession_var.set(label)
        self.human_profession_combo.configure(
            state="readonly" if selected and not any(h.edit_error for h in selected) else tk.DISABLED,
        )
        self._update_human_action()
        if len(selected) != 1:
            text = (f"{len(selected)} humans selected. The chosen profession is queued for all of them and "
                    "replaces their profession traits on save; mutations and other traits are kept."
                    if selected else "Select one human to view saved attributes; Ctrl/Shift-click selects several.")
            if any(h.edit_error for h in selected):
                text += " Selection includes read-only humans; select one to see why."
            self.human_details_var.set(text)
            return
        human = selected[0]
        text = f"Social ID: {human.social_id} | {human.location}"
        if human.timestamp is not None:
            text += f" | Saved timestamp: {human.timestamp}"
        if human.delivery_timestamp is not None:
            text += f" | Delivery timestamp: {human.delivery_timestamp}"
        if human.key in self.human_pending:
            text += " | Unsaved profession"
        if human.pod:
            text += f"\nPod: {human.pod.actor}"
        if human.edit_error:
            text += f"\nRead-only: {human.edit_error}"
        self.human_details_var.set(text)
        current = human.pod.attributes_current if human.pod else {}
        simulated = human.pod.attributes_simulated if human.pod else {}
        for asset in sorted(human.attributes.keys() | current.keys() | simulated.keys(), key=human_io.asset_label):
            values = []
            for source, saved in zip(human_io.ATTRIBUTE_SOURCES, (human.attributes, current, simulated)):
                key = human.key, source, asset
                if key in self.human_attribute_pending:
                    values.append(_format_value(self.human_attribute_pending[key]) + " *")
                else:
                    values.append(_format_value(saved[asset]) if asset in saved else "-")
            self.human_attribute_tree.insert("", tk.END, iid=asset, values=(
                human_io.asset_label(asset), *values,
            ))
        self._apply_saved_sort(self.human_attribute_tree)
        self.human_attribute_tree.selection_set(
            [asset for asset in attributes_selected if self.human_attribute_tree.exists(asset)])
        self.human_attribute_tree.yview_moveto(attribute_scroll)
        self._update_human_action()

    @_not_while_pumping
    def _update_human_action(self, _event=None) -> None:
        state = tk.NORMAL if self.human_pending or self.human_attribute_pending else tk.DISABLED
        self.human_save_button.configure(state=state)
        self.human_discard_button.configure(state=state)
        selected = [self.human_rows[iid] for iid in self.human_tree.selection() if iid in self.human_rows]
        editable = (self.document and not self.document.human_data.error
                    and len(selected) == 1 and not selected[0].edit_error
                    and len(self.human_attribute_tree.selection()) == 1)
        self.human_stat_button.configure(state=tk.NORMAL if editable else tk.DISABLED)

    @_not_while_pumping
    def _edit_human_attribute(self, event: tk.Event | None = None) -> None:
        selected = [self.human_rows[iid] for iid in self.human_tree.selection() if iid in self.human_rows]
        if len(selected) != 1 or not self.document:
            return
        human = selected[0]
        if self.document.human_data.error or human.edit_error:
            messagebox.showerror("Human editing unavailable", self.document.human_data.error or human.edit_error)
            return
        focus_source = None
        if event is not None and event.type == tk.EventType.ButtonPress:
            tree = self.human_attribute_tree
            if tree.identify_region(event.x, event.y) != "cell":
                return
            tree.selection_set(tree.identify_row(event.y))
            column = tree.identify_column(event.x)
            sources: dict[str, human_io.AttributeSource] = {
                "#2": "roster", "#3": "current", "#4": "simulated",
            }
            focus_source = sources.get(column)
        selection = self.human_attribute_tree.selection()
        if len(selection) != 1:
            return
        asset = selection[0]
        initial = {
            source: self.human_attribute_pending.get((human.key, source, asset), site.values[asset])
            for source, site in human.attribute_sets.items() if asset in site.values
        }
        if not initial:
            messagebox.showerror("Stat unavailable", "This stat is not stored for the selected human. Reload the save.")
            return
        if focus_source is not None and focus_source not in initial:
            messagebox.showinfo(
                "Stat unavailable", f"{human_io.ATTRIBUTE_SOURCES[focus_source]} is not stored for this stat.",
            )
            return
        dialog = _HumanStatDialog(self, human, asset, initial, focus_source)
        if dialog.result is None:
            return
        for source, value in dialog.result.items():
            key = human.key, source, asset
            if value == human.attribute_sets[source].values[asset]:
                self.human_attribute_pending.pop(key, None)
            else:
                self.human_attribute_pending[key] = value
        self._refresh_human_rows()
        if any(key[0] == human.key and key[2] == asset for key in self.human_attribute_pending):
            self.status_var.set(f"Queued raw {human_io.asset_label(asset)} changes. Save Changes writes all queued edits.")
        else:
            self.status_var.set(f"{human_io.asset_label(asset)} matches the saved values; nothing queued for this stat.")

    @_not_while_pumping
    def _populate_committees(self) -> None:
        self.committee_tree.delete(*self.committee_tree.get_children())
        if not self.document or self.document.human_data.error:
            return
        data = self.document.human_data
        roles = human_io.committee_coverage(data, self.human_pending)
        counts = Counter((r.world, r.committee.key) for r in roles if r.social_ids)
        for role in roles:
            values = (
                role.committee.name, f"{counts[role.world, role.committee.key]}/4",
                role.profession, len(role.social_ids), "Present" if role.social_ids else "Missing",
                role.committee.tier, role.saved_state.rsplit("::", 1)[-1], role.world,
                "; ".join(role.social_ids) or "-",
            )
            if self._matches_filter(self.committee_filter_applied, " ".join(map(str, values))):
                iid = "role:" + json.dumps((role.world, role.committee.key, role.profession))
                self.committee_tree.insert("", tk.END, iid=iid, values=values)
        known = {committee.key for committee in human_io.COMMITTEES}
        for (world, key), state in data.committee_states.items():
            if key in known:
                continue
            values = (key, "Unknown", "Requirements not in v6 catalogue", "-", "Unknown",
                      "-", state.rsplit("::", 1)[-1], world, "-")
            if self._matches_filter(self.committee_filter_applied, " ".join(map(str, values))):
                self.committee_tree.insert("", tk.END, iid="unknown:" + json.dumps((world, key)), values=values)
        self._apply_saved_sort(self.committee_tree)

    @_not_while_pumping
    def _queue_human_profession(self, _event=None) -> None:
        """Queue the chosen profession for the selected humans; Save Changes writes the queue."""
        selected = [self.human_rows[iid] for iid in self.human_tree.selection() if iid in self.human_rows]
        label = self.human_profession_var.get()
        if not selected or not self.document or self.document.human_data.error:
            return
        if label != NO_HUMAN_PROFESSION and label not in human_io.PROFESSION_ASSETS:
            return
        blocked = [human for human in selected if human.edit_error]
        if blocked:
            messagebox.showerror(
                "Human editing unavailable", "\n".join(f"{h.social_id}: {h.edit_error}" for h in blocked),
            )
            self._show_human_selection()
            return
        profession = None if label == NO_HUMAN_PROFESSION else human_io.PROFESSION_ASSETS[label]
        queued = removed = 0
        for human in selected:
            if human_io.profession_differs(human, profession):
                self.human_pending[human.key] = profession
                queued += 1
            elif self.human_pending.pop(human.key, False) is not False:
                removed += 1
        self._refresh_human_rows()
        if queued:
            self.status_var.set(
                f"Queued {label} for {queued} human(s). {len(self.human_pending)} profession "
                "change(s) wait for Save Changes."
            )
        elif removed:
            self.status_var.set(f"{label} matches the saved profession; removed {removed} queued change(s).")
        else:
            self.status_var.set(f"{label} is already saved for the selected human(s); nothing queued.")

    def _refresh_human_rows(self) -> None:
        """Show queued human edits in place, keeping the roster's scroll position."""
        for iid, human in self.human_rows.items():
            self.human_tree.item(iid, values=self._human_values(human))
        self._apply_saved_sort(self.human_tree)
        self._update_human_summary()
        self._show_human_selection()
        self._populate_committees()
        self._update_pending()

    @_not_while_pumping
    def _discard_human_changes(self) -> None:
        professions, stats = len(self.human_pending), len(self.human_attribute_pending)
        count = professions + stats
        if not count or not messagebox.askyesno(
            "Discard human changes?",
            f"Discard {professions} unsaved profession change(s) and {stats} unsaved stat field(s)?",
        ):
            return
        self.human_pending.clear()
        self.human_attribute_pending.clear()
        self._refresh_human_rows()
        self.status_var.set(f"Discarded {count} unsaved human change(s)")

    def _build_scalars(self) -> None:
        toolbar = ttk.Frame(self.scalars_tab)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        self._build_filters(toolbar, "scalar", self._apply_scalar_filter)
        ttk.Button(toolbar, text="Edit Selected...", command=self._edit_scalar).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Set Selected to...", command=self._set_selected_scalars).pack(side=tk.LEFT, padx=2)
        self.scalar_tree = self._make_tree(self.scalars_tab, SCALAR_COLUMNS, search_column="name")
        self.scalar_tree.bind("<Double-1>", self._edit_scalar)

    def _build_resources(self) -> None:
        toolbar = ttk.Frame(self.resources_tab)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        self._build_filters(toolbar, "resource", self._apply_resource_filter)
        ttk.Button(toolbar, text="Edit Selected...", command=self._edit_resources).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            toolbar, text="Edit Health...", command=lambda: self._edit_resource_condition("health"),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            toolbar, text="Edit Extra Damage...", command=lambda: self._edit_resource_condition("extra_damage"),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Label(
            self.resources_tab,
            text="Saved fuel / electricity / other resource amounts, not stack counts. "
            "Health/damage are raw values, not percentages. UAID world actors are hidden. Edit, then Save Changes.",
        ).pack(anchor=tk.W, pady=(0, 6))
        self.resource_tree = self._make_tree(
            self.resources_tab, RESOURCE_COLUMNS, search_column="actor",
        )
        self.resource_tree.bind("<Double-1>", self._edit_resources)

    def _build_backups(self) -> None:
        toolbar = ttk.Frame(self.backups_tab)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(toolbar, text="Create Backup Now", command=self._create_backup).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Restore Selected", command=self._restore_backup).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Open Save Folder", command=self._open_folder).pack(side=tk.LEFT, padx=2)
        columns = (("time", "Created", 180), ("size", "Size", 110), ("path", "Backup", 800))
        self.backup_tree = self._make_tree(self.backups_tab, columns)

    def _refresh_saves(self) -> None:
        self.candidates = save_io.discover_saves()
        labels = [str(path) for path in self.candidates]
        self.save_combo["values"] = labels
        if self.document and self.document.path in self.candidates:
            self.save_var.set(str(self.document.path))
        elif labels:
            self.save_var.set(labels[0])
        self.status_var.set(f"Found {len(labels)} save(s)")

    def _select_save(self, _event: tk.Event | None = None) -> None:
        value = self.save_var.get()
        if value:
            self._open_save(Path(value))

    def _open(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a The Last Caretaker save",
            initialdir=save_io.DEFAULT_SAVE_ROOT,
            filetypes=(("The Last Caretaker saves", "*.sav"), ("All files", "*.*")),
        )
        if path:
            self._open_save(Path(path))

    def _load_startup_save(self) -> None:
        if not self.auto_load_latest_var.get():
            self.status_var.set("Choose a save or use Browse... to open one")
            return
        path = self.candidates[0] if self.candidates else None
        if path is not None:
            self._open_save(path, startup=True)

    def _open_save(self, path: Path, *, confirm_discard: bool = True, startup: bool = False) -> bool:
        """User-facing loads run automation; internal post-write loads never do."""
        if not self._load(path, confirm_discard=confirm_discard):
            return False
        if startup:
            self._create_backup()
        if self.repair_on_open_var.get():
            self._repair_and_top_off(automatic=True)
        return True

    def _open_full_screen(self) -> None:
        geometry = self.settings.get("geometry")
        if geometry:
            try:
                self.geometry(geometry)
            except tk.TclError:
                pass
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.update_idletasks()

    @_not_while_pumping
    def _load(
        self,
        path: Path,
        *,
        confirm_discard: bool = True,
    ) -> bool:
        if (
            confirm_discard
            and _unsaved_edits(self)
            and not messagebox.askyesno(
                "Discard edits?", "Discard pending edits and open another save?"
            )
        ):
            return False
        same_save = bool(self.document and self.document.path == path)
        views = self._capture_views() if same_save else {}
        failure: Exception | None = None
        with _busy_ui(self, f"Loading {path.name}...") as busy:
            try:
                document = _run_in_background(self, save_io.load_save, path, report=True)
            except Exception as exc:
                failure = exc
            else:
                busy("Building the tables...")
                self.document = document
                self.pending.clear()
                self.human_pending = {}
                self.human_attribute_pending = {}
                self.save_var.set(str(path))
                self._populate_progress()
                self._populate_inventory()
                self._populate_humans()
                self._reset_map()
                self._populate_scalars()
                self._populate_resources()
                self._populate_backups()
                self._update_pending()
                if views:
                    self._restore_views(views)
                    self._show_human_selection()
        if failure is not None:
            messagebox.showerror("Could not open save", str(failure))
            return False
        status = (
            f"Loaded {path.name}: {len(self.document.entries):,} editable scalars, "
            f"{len(self.document.items):,} inventory records"
        )
        warnings = getattr(self.document, "warnings", None)
        if warnings:
            status += " | WARNING: " + "; ".join(warnings)
        human_error = self.document.human_data.error
        if human_error:
            status += " | WARNING: " + human_error
        self.status_var.set(status)
        self.title(f"The Last Caretaker Save Editor - {path.name}")
        if warnings:
            messagebox.showwarning(
                "Inventory structure",
                f"{path.name} loaded, but " + "; ".join(warnings) + ".\n\n"
                "Review item locations before using Repair & Top Off, Remove Duplicate "
                "Stacks, Purge or Delete Items on this save.",
            )
        if human_error:
            messagebox.showwarning(
                "Human data", human_error + "\n\nHuman editing is disabled. Other editor tabs remain available.",
            )
        self._map_tab_changed()
        return True

    def _trees(self) -> tuple[ttk.Treeview, ...]:
        return (
            self.progress_tree, self.inventory_tree, self.resource_tree,
            self.scalar_tree, self.backup_tree, self.human_tree,
            self.human_attribute_tree, self.committee_tree,
        )

    def _capture_views(self) -> dict[int, tuple[tuple[str, ...], str, float]]:
        state: dict[int, tuple[tuple[str, ...], str, float]] = {}
        for tree in self._trees():
            state[id(tree)] = (tuple(tree.selection()), tree.focus(), tree.yview()[0])
        return state

    def _restore_views(self, state: dict[int, tuple[tuple[str, ...], str, float]]) -> None:
        for tree in self._trees():
            saved = state.get(id(tree))
            if not saved:
                continue
            selection, focus, offset = saved
            rows = set(tree.get_children())
            keep = [iid for iid in selection if iid in rows]
            if keep:
                tree.selection_set(keep)
            if focus in rows:
                tree.focus(focus)
            try:
                tree.yview_moveto(offset)
            except tk.TclError:
                pass

    @_not_while_pumping
    def _reload(self, *, prompt: bool = False) -> None:
        if not self.document:
            return
        if (
            prompt
            and _unsaved_edits(self)
            and not messagebox.askyesno(
                "Discard edits?",
                f"Reloading discards {_unsaved_edits(self)} unsaved edit(s).\n\nReload from disk anyway?",
            )
        ):
            self.status_var.set("Reload canceled; pending edits kept")
            return
        self._open_save(self.document.path, confirm_discard=False)
        self.status_var.set(f"Reloaded {self.document.path.name} from disk")

    def _display_value(self, entry: save_io.Entry | None) -> str:
        if entry is None:
            return ""
        return _format_value(self.pending.get(entry.offset, entry.value))

    def _populate_progress(self) -> None:
        self.progress_tree.delete(*self.progress_tree.get_children())
        if not self.document:
            return
        by_name: dict[str, save_io.Entry] = {}
        for entry in self.document.entries:
            by_name.setdefault(entry.name, entry)
        for name, label in PROGRESS_FIELDS:
            entry = by_name.get(name)
            if entry:
                self.progress_tree.insert(
                    "", tk.END, iid=str(entry.offset),
                    values=(label, self._display_value(entry), entry.type, f"0x{entry.offset:X}"),
                )
        for label, entry in save_io.player_stat_entries(self.document):
            self.progress_tree.insert(
                "", tk.END, iid=str(entry.offset),
                values=(
                    f"Player {_player_stat_label(label)}",
                    self._display_value(entry),
                    entry.type,
                    f"0x{entry.offset:X}",
                ),
            )
        for label, entry in save_io.enhancer_points_entries(self.document):
            self.progress_tree.insert(
                "", tk.END, iid=str(entry.offset),
                values=(f"Enhancer: {label}", self._display_value(entry), entry.type, f"0x{entry.offset:X}"),
            )
        try:
            locks = save_io.player_lock_states(self.document)
        except Exception:
            locks = {}
        for key, var in self.player_lock_vars.items():
            var.set(bool(locks.get(key)))
            self.player_lock_buttons[key].configure(state=tk.NORMAL if key in locks else tk.DISABLED)
        active = [save_io.PLAYER_LOCKS[key]["label"] for key, on in locks.items() if on]
        player_labels = [label.casefold() for label, _entry in save_io.player_stat_entries(self.document)]
        if active:
            self.charge_status_var.set(
                f"{' and '.join(active)} locked in this save (minimum pinned at or above the maximum, "
                "as left by a trainer). Untick below to restore normal behaviour."
            )
        elif len(locks) < len(save_io.PLAYER_LOCKS):
            self.charge_status_var.set(
                "Editable survival limits are not serialized in this save. "
                "Infinite Health / Energy controls without saved limits are disabled; "
                "use Boost Health/Energy Enhancers to raise maximums instead. "
                "Current survival values, when present, remain editable below."
            )
        elif any(label.startswith("charge -") for label in player_labels):
            self.charge_status_var.set("Robot Charge is serialized in this save and is editable below.")
        else:
            self.charge_status_var.set(
                "Robot Charge is not serialized in this save. The game defines the Charge stat, "
                "but this character's SurvivalAttributes map currently contains only Health and Level "
                "(Level is the energy meter)."
            )

    def _apply_item_filter(self) -> None:
        self._apply_filters("item", self._populate_inventory)

    def _clear_item_filter(self) -> None:
        self._clear_filters("item", self._apply_item_filter)

    def _apply_scalar_filter(self) -> None:
        self._apply_filters("scalar", self._populate_scalars)

    def _clear_scalar_filter(self) -> None:
        self._clear_filters("scalar", self._apply_scalar_filter)

    def _apply_resource_filter(self) -> None:
        self._apply_filters("resource", self._populate_resources)

    def _clear_resource_filter(self) -> None:
        self._clear_filters("resource", self._apply_resource_filter)

    def _populate_resources(self) -> None:
        self.resource_tree.delete(*self.resource_tree.get_children())
        self.resource_rows.clear()
        self.resource_records.clear()
        if not self.document:
            return
        query = self.resource_filter_applied
        for record in self.document.resources:
            if "_c_uaid_" in record.actor.casefold() or "npc" in record.actor.casefold():
                continue
            searchable = " ".join((
                record.resource, record.entry.name, record.entry.type,
                str(self.pending.get(record.entry.offset, record.entry.value)),
                record.asset, record.location, record.actor,
                record.health.name if record.health else "",
                record.health.type if record.health else "",
                str(self.pending.get(record.health.offset, record.health.value)) if record.health else "",
                record.extra_damage.name if record.extra_damage else "",
                record.extra_damage.type if record.extra_damage else "",
                str(self.pending.get(record.extra_damage.offset, record.extra_damage.value))
                if record.extra_damage else "",
                f"0x{record.entry.offset:X} 0x{record.map_offset:X}",
            ))
            if not self._matches_filter(query, searchable):
                continue
            iid = str(record.entry.offset)
            self.resource_rows[iid] = record.entry
            self.resource_records[iid] = record
            self.resource_tree.insert("", tk.END, iid=iid, values=(
                record.resource, self._display_value(record.entry),
                self._display_value(record.health), self._display_value(record.extra_damage), record.asset,
                record.location, record.actor, f"0x{record.entry.offset:X}",
            ))
        self._apply_saved_sort(self.resource_tree)

    def _edit_resources(self, _event: tk.Event | None = None) -> None:
        if _event is not None:
            column = self.resource_tree.identify_column(_event.x)
            if column in {"#3", "#4"}:
                self._edit_resource_condition("health" if column == "#3" else "extra_damage")
                return
        entries = [
            self.resource_rows[iid] for iid in self.resource_tree.selection()
            if iid in self.resource_rows
        ]
        if len(entries) == 1:
            self._prompt_entry(entries[0])
        elif entries:
            self._prompt_entries(entries)

    def _edit_resource_condition(self, field: str) -> None:
        entries = {
            entry.offset: entry
            for iid in self.resource_tree.selection()
            if iid in self.resource_records
            for entry in [getattr(self.resource_records[iid], field)]
            if entry is not None
        }
        if len(entries) == 1:
            self._prompt_entry(next(iter(entries.values())))
        elif entries:
            self._prompt_entries(list(entries.values()))
        else:
            messagebox.showinfo("No saved condition", "No unambiguous saved condition field exists for this selection.")

    def _populate_inventory(self) -> None:
        self.inventory_tree.delete(*self.inventory_tree.get_children())
        self.item_rows.clear()
        if not self.document:
            return
        query = self.item_filter_applied
        location = self.location_filter.get()
        visible = []
        for item in self.document.items:
            if location == "Carried (all)":
                if not save_io.is_carried(item.location):
                    continue
            elif location != "All" and item.location != location:
                continue
            count = int(self.pending.get(item.entries["ItemCount"].offset, item.count))
            if count == 0 and not self.show_empty_items.get():
                continue
            searchable = " ".join((
                item.asset, item.category, item.actor, item.location,
                str(item.index), f"0x{item.anchor_offset:X}",
                *(
                    f"{entry.name} {entry.type} "
                    f"{self.pending.get(entry.offset, entry.value)} {entry.context} "
                    f"0x{entry.offset:X}"
                    for entry in item.entries.values()
                ),
            ))
            if not self._matches_filter(query, searchable):
                continue
            visible.append(item)
        visible.sort(key=lambda row: (row.category.casefold(), row.asset.casefold()))
        for item in visible:
            iid = str(item.index)
            self.item_rows[iid] = item
            self.inventory_tree.insert("", tk.END, iid=iid, values=(
                item.asset, item.location, item.category,
                self._display_value(item.entries.get("ItemCount")),
                self._display_value(item.entries.get("Health")),
                self._display_value(item.entries.get("SelectedVariation")),
                item.actor,
            ), tags=(_inventory_type_tag(item),))
        self._apply_saved_sort(self.inventory_tree)

    def _populate_scalars(self) -> None:
        self.scalar_tree.delete(*self.scalar_tree.get_children())
        self.scalar_rows.clear()
        if not self.document:
            return
        query = self.scalar_filter_applied
        for entry in self.document.entries:
            searchable = (
                f"{entry.name} {entry.type} {self.pending.get(entry.offset, entry.value)} "
                f"{entry.context} 0x{entry.offset:X} {entry.index}"
            )
            if not self._matches_filter(query, searchable):
                continue
            iid = str(entry.index)
            self.scalar_rows[iid] = entry
            self.scalar_tree.insert("", tk.END, iid=iid, values=(
                entry.index, entry.name, entry.type, self._display_value(entry),
                entry.context, f"0x{entry.offset:X}",
            ))
        self._apply_saved_sort(self.scalar_tree)

    def _populate_backups(self) -> None:
        self.backup_tree.delete(*self.backup_tree.get_children())
        if not self.document:
            return
        for index, path in enumerate(save_io.list_backups(self.document.path)):
            stat = path.stat()
            self.backup_tree.insert("", tk.END, iid=str(index), values=(
                datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                f"{stat.st_size:,}", str(path),
            ))

    def _refresh_values(self) -> None:
        """Update displayed values in place so rows never move or re-sort."""
        if not self.document:
            return
        for iid in self.progress_tree.get_children():
            entry = next((e for e in self.document.entries if e.offset == int(iid)), None)
            if entry is not None:
                self.progress_tree.set(iid, "value", self._display_value(entry))
        for iid in self.inventory_tree.get_children():
            item = self.item_rows.get(iid)
            if item is None:
                continue
            self.inventory_tree.set(iid, "count", self._display_value(item.entries.get("ItemCount")))
            self.inventory_tree.set(iid, "health", self._display_value(item.entries.get("Health")))
            self.inventory_tree.set(iid, "variation", self._display_value(item.entries.get("SelectedVariation")))
        for iid in self.scalar_tree.get_children():
            entry = self.scalar_rows.get(iid)
            if entry is not None:
                self.scalar_tree.set(iid, "value", self._display_value(entry))
        for iid in self.resource_tree.get_children():
            entry = self.resource_rows.get(iid)
            if entry is not None:
                self.resource_tree.set(iid, "value", self._display_value(entry))
            record = self.resource_records.get(iid)
            if record:
                self.resource_tree.set(iid, "health", self._display_value(record.health))
                self.resource_tree.set(iid, "damage", self._display_value(record.extra_damage))
        self._update_pending()

    def _queue(self, entry: save_io.Entry, value: object) -> None:
        parsed = save_io.parse_value(entry, value)
        if parsed == entry.value:
            self.pending.pop(entry.offset, None)
        else:
            self.pending[entry.offset] = parsed
        self._refresh_values()

    def _edit_progress(self, _event: tk.Event | None = None) -> None:
        selection = self.progress_tree.selection()
        if not selection or not self.document:
            return
        offsets = {int(iid) for iid in selection}
        entries = [entry for entry in self.document.entries if entry.offset in offsets]
        if not entries:
            return
        if len(entries) == 1:
            self._prompt_entry(entries[0])
            return
        self._prompt_entries(entries)

    def _quick_edit(self, name: str) -> None:
        if not self.document:
            return
        entry = next((entry for entry in self.document.entries if entry.name == name), None)
        if entry:
            self._prompt_entry(entry)

    def _boost_survival_enhancers(self) -> None:
        """Stage one value on the Vitality, Battery and Power Management enhancers."""
        if not self.document:
            return
        found = save_io.survival_enhancer_entries(self.document)
        if not found:
            messagebox.showinfo(
                "Enhancers not found",
                "This save has no Vitality, Battery or Power Management enhancer Points to edit.",
            )
            return
        names = ", ".join(label for label, _entry in found)
        value = simpledialog.askstring(
            "Boost health/energy enhancers",
            f"New Points value for {names}:\n"
            "(Higher points raise maximum health and energy. Review, then Save.)",
            initialvalue=str(save_io.SURVIVAL_ENHANCER_BOOST),
            parent=self,
        )
        if value is None:
            return
        try:
            parsed = [(entry, save_io.parse_value(entry, value)) for _label, entry in found]
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))
            return
        for entry, new_value in parsed:
            if new_value == entry.value:
                self.pending.pop(entry.offset, None)
            else:
                self.pending[entry.offset] = new_value
        self._refresh_values()
        self.status_var.set(f"Staged {value.strip()} enhancer points for {names}; Save to apply.")

    def _prompt_entry(self, entry: save_io.Entry) -> None:
        value = simpledialog.askstring(
            "Edit value", f"{entry.name} ({entry.type})", initialvalue=self._display_value(entry), parent=self
        )
        if value is None:
            return
        try:
            self._queue(entry, value)
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))

    def _prompt_entries(self, entries: list[save_io.Entry]) -> None:
        """Set the same value on every selected row at once (multi-select edit)."""
        types = {entry.type for entry in entries}
        if len(types) > 1:
            messagebox.showerror(
                "Mixed types selected",
                "The selected rows have different value types "
                f"({', '.join(sorted(types))}); select rows of one type to bulk-edit.",
            )
            return
        value = simpledialog.askstring(
            "Edit value",
            f"New value for {len(entries)} selected field(s) ({entries[0].type}):",
            parent=self,
        )
        if value is None:
            return
        try:
            parsed = [(entry, save_io.parse_value(entry, value)) for entry in entries]
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))
            return
        for entry, new_value in parsed:
            if new_value == entry.value:
                self.pending.pop(entry.offset, None)
            else:
                self.pending[entry.offset] = new_value
        self._refresh_values()

    def _selected_items(self) -> list[save_io.InventoryItem]:
        return [self.item_rows[iid] for iid in self.inventory_tree.selection() if iid in self.item_rows]

    def _set_item_field(self, field: str, label: str) -> None:
        items = self._selected_items()
        if not items:
            return
        editable = [item.entries[field] for item in items if field in item.entries]
        if not editable:
            messagebox.showinfo("Field unavailable", f"None of the selected records has an editable {label}.")
            return
        value = simpledialog.askstring(
            f"Set item {label}",
            f"New {label} for {len(editable)} selected item(s):",
            parent=self,
        )
        if value is None:
            return
        try:
            parsed = [(entry, save_io.parse_value(entry, value)) for entry in editable]
            for entry, new_value in parsed:
                if new_value == entry.value:
                    self.pending.pop(entry.offset, None)
                else:
                    self.pending[entry.offset] = new_value
            self._refresh_values()
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))

    def _sync_document(self) -> bool:
        """Reload when the game rewrote the save after the editor loaded it.

        Without this, edits are computed against stale counts (so an item that
        was already at its target when loaded produces no edit at all) and the
        write then fails its own on-disk consistency check.
        """
        if not self.document:
            return False
        try:
            with _busy_ui(self, "Checking the save for outside changes..."):
                stale = _run_in_background(self, save_io.save_is_stale, self.document)
            if not stale:
                return True
        except Exception:
            return True
        if _unsaved_edits(self) and not messagebox.askyesno(
            "Save changed on disk",
            "The game has written this save since the editor loaded it.\n\n"
            "Reload the current file and discard the pending edits?",
        ):
            return False
        if not self._load(self.document.path, confirm_discard=False):
            return False
        self.status_var.set("Reloaded: the save had changed on disk")
        return True

    @_not_while_pumping
    def _repair_and_top_off(self, *, automatic: bool = False) -> None:
        if not self.document:
            return
        if automatic and _run_in_background(self, _game_is_running):
            self.status_var.set("Automatic Repair & Top Off skipped: the game is running")
            messagebox.showerror("Game is running", "Close The Last Caretaker before automatically repairing its save.")
            return
        if getattr(self, "human_pending", None) or getattr(self, "human_attribute_pending", None):
            messagebox.showwarning(
                "Unsaved human changes",
                "Repair & Top Off saves the file without asking about queued human changes.\n\n"
                "Save Changes or Discard Human Changes on the Humans tab first.",
            )
            return
        if not self._sync_document():
            return
        selected = [] if automatic else self._selected_items()
        if selected:
            targets = selected
            non_carried = sum(
                1 for item in targets
                if not save_io.is_carried(item.location)
                and save_io.repair_category(item.asset, item.category) not in save_io._REPAIR_EVERYWHERE
            )
            scope = f"{len(targets)} selected record(s)"
            if non_carried:
                scope += f" ({non_carried} not carried -> stack of 1)"
        else:
            targets = [item for item in self.document.items if save_io.is_carried(item.location)]
            scope = "every carried item (backpack, holster and equipped)"
        owned = [
            item for item in targets
            if int(self.pending.get(item.entries["ItemCount"].offset, item.count)) > 0
        ]
        configured = save_io.normalize_repair_targets(self.repair_targets)
        storage_edits = (
            save_io.storage_refill_edits(
                self.document, configured["storage.tanks"],
                configured["storage.health"], configured["storage.extra_damage"],
            )
            if self.refill_storage_var.get() else []
        )
        seed_edits = save_io.human_seed_time_edits(self.document)
        rocket_edits = (
            save_io.rocket_resource_edits(self.document, configured)
            if self.refill_rocket_var.get() else []
        )
        if not owned and not storage_edits and not seed_edits and not rocket_edits:
            messagebox.showinfo(
                "Nothing to repair",
                "No carried items were found. Select records explicitly to also "
                "act on the recipe catalog, module mirror, environment or "
                "container records.",
            )
            return
        overrides = save_io.normalize_asset_targets(self.repair_asset_targets)
        special = ", ".join(
            f"{' / '.join(label)} {configured[key]}"
            for key, label, _default, everywhere in save_io.REPAIR_CATEGORIES if everywhere
        )
        per_item = (
            f"{len(overrides)} per-item stack size(s) are configured and win over "
            "their category.\n"
            if overrides else ""
        )
        if not automatic and self.confirm_repair_var.get() and not messagebox.askyesno(
            "Repair and top off?",
            f"Refill and repair {scope}?\n\n"
            f"{per_item}"
            "Carried items (backpack, holster, equipped) are refilled to the "
            "configured stack target for their category (Settings > Stack Targets...). "
            f"These apply wherever the item is: {special}. Attachments "
            "installed on a weapon are left untouched. Anything else not "
            "carried by the player (environment, containers, catalog, module "
            "mirror) is compacted to the Not Carried target "
            f"({configured['not_carried']}) instead of being topped off.\n"
            f"{len(getattr(self, 'repair_delete_items', []))} exact item name(s) "
            "are marked for carried-item deletion in Delete Items.... "
            "There are no built-in Trash, Scrap, or Bio deletion rules.\n"
            "Health is restored to the highest known value for each inventory item.\n"
            f"Set {len(seed_edits)} HumanSeed TimeLeft float field(s) to 99999.\n"
            f"Update {len(storage_edits)} container field(s): Stored Amount "
            f"{configured['storage.tanks']}, Health {configured['storage.health']}, "
            f"ExtraDamage {configured['storage.extra_damage']} (Refill Storage Tanks).\n"
            "Every existing Resources.* amount in All Scalars uses the Storage target, "
            "including NPCs, hidden world actors and backpack Electricity, except the rocket's "
            "Electricity, Methane and Oxygen. "
            "Condition-field protections remain unchanged.\n"
            f"Set {len(rocket_edits)} Functional Rocket resource field(s): "
            + ", ".join(
                f"{name} {configured[f'rocket.{name.casefold()}']}"
                for name in save_io.ROCKET_RESOURCE_TARGETS
            )
            + (" (Top Off Rocket Resources enabled; overrides all other repair amounts)."
               if self.refill_rocket_var.get() else
               " (Top Off Rocket Resources disabled; these rocket resources are unchanged)."),
        ):
            return
        with _busy_ui(self, "Processing Repair & Top Off...") as progress:
            LastCaretakerEditor._execute_repair_and_top_off(
                self, owned, configured, overrides, storage_edits, progress, seed_edits,
                rocket_edits,
            )

    def _execute_repair_and_top_off(
        self, owned, configured, overrides, storage_edits, progress, seed_edits=(),
        rocket_edits=(),
    ) -> None:
        progress("Calculating Repair & Top Off changes...")
        edits = save_io.repair_and_top_off(self.document, owned, configured, overrides)
        edits.extend(seed_edits)
        edits.extend(storage_edits)
        edits.extend(rocket_edits)
        for entry, value in edits:
            if value == entry.value:
                self.pending.pop(entry.offset, None)
            else:
                self.pending[entry.offset] = value
        self._refresh_values()
        self.status_var.set(
            f"Repaired and topped off {len(owned)} item(s), refilled {len(storage_edits)} storage field(s) "
            f"and {len(rocket_edits)} rocket field(s) ({len(edits)} value change(s))")
        progress("Saving repaired and refilled values...")
        if not self._save(confirm=False):
            return
        if getattr(self, "repair_delete_items", []):
            progress("Removing items from the configured deletion list...")
            self._remove_configured_items()
        if self.repair_combine_var.get():
            progress("Finding and removing duplicate stacks...")
            self._remove_duplicate_stacks(confirm=False, include_memory=True)

    def _remove_configured_items(self) -> None:
        """Delete only explicitly configured carried items after a successful save."""
        if not self.document or not self.repair_delete_items:
            return
        items = list(self.repair_delete_items)
        try:
            records = _run_in_background(self, save_io.deletion_records, self.document, items)
        except Exception as exc:
            messagebox.showerror("Configured deletion failed", str(exc))
            return
        if not records:
            return
        if not self._structural_guard():
            return
        if self.confirm_delete_var.get() and not messagebox.askyesno(
            "Delete configured items?",
            f"Delete {len(records)} carried record(s) matching {len(items)} "
            "configured item name(s)?\n\n" + "\n".join(items[:15])
            + ("\n..." if len(items) > 15 else ""),
        ):
            self.status_var.set("Configured item deletion skipped")
            return
        keep = max(1, self.keep_var.get())
        self._purge(lambda doc: save_io.purge_items(
            doc, save_io.deletion_records(doc, items), player_only=False,
            make_backup=False, max_backups=keep))

    def _add_item(self) -> None:
        if not self.document:
            return
        catalog = save_io.player_item_catalog(self.document)
        if not catalog:
            messagebox.showinfo("No item catalog", "No player item templates were found in this save.")
            return
        dialog = tk.Toplevel(self)
        dialog.title("Add Item to Backpack")
        dialog.transient(self)
        dialog.resizable(True, False)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Quick filter:").grid(row=0, column=0, sticky="w")
        filter_var = tk.StringVar()
        filter_entry = ttk.Entry(frame, textvariable=filter_var, width=52)
        filter_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        def refresh_choices() -> None:
            query = filter_var.get().strip().casefold()
            items = tuple(catalog) if not query else tuple(
                asset for asset in catalog if query in asset.casefold()
            )
            item_combo.configure(values=items)
            if not items:
                item_var.set("")
                return
            if item_var.get() not in items:
                item_var.set(items[0])

        item_var = tk.StringVar(value=next(iter(catalog)))
        item_combo = ttk.Combobox(
            frame,
            textvariable=item_var,
            values=tuple(catalog),
            width=52,
        )
        item_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        filter_var.trace_add("write", lambda *_: refresh_choices())
        refresh_choices()
        ttk.Label(frame, text="Item:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(frame, text="Quantity:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        quantity_var = tk.StringVar(value="1")
        quantity_entry = ttk.Entry(frame, textvariable=quantity_var, width=12)
        quantity_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        result: list[tuple[str, int]] = []

        def accept() -> None:
            asset = item_var.get().strip()
            if asset not in catalog:
                messagebox.showerror("Unknown item", "Choose an item from the player catalog.", parent=dialog)
                return
            try:
                quantity = int(quantity_var.get(), 0)
                if quantity < 1 or quantity >= 2**31:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid quantity", "Quantity must be a positive 32-bit integer.", parent=dialog)
                return
            result.append((asset, quantity))
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Add", command=accept).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=2)
        frame.columnconfigure(1, weight=1)
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        item_combo.focus_set()
        self.wait_window(dialog)
        if not result:
            return
        asset, quantity = result[0]
        rows = catalog[asset]
        target = next(
            (
                item for item in rows
                if int(self.pending.get(item.entries["ItemCount"].offset, item.count)) > 0
            ),
            rows[0],
        )
        entry = target.entries["ItemCount"]
        current = int(self.pending.get(entry.offset, entry.value))
        new_count = current + quantity if current > 0 else quantity
        if new_count >= 2**31:
            messagebox.showerror(
                "Quantity too large",
                "The resulting item count must fit in a signed 32-bit integer.",
            )
            return
        self.pending[entry.offset] = new_count
        if str(target.index) in self.item_rows:
            self._refresh_values()
        else:
            self._populate_inventory()
            self._update_pending()
        self.status_var.set(f"Queued {quantity} x {asset} to the player inventory")

    def _delete_items(self) -> None:
        items = self._selected_items()
        if not items:
            return
        if self.confirm_delete_var.get() and not messagebox.askyesno(
            "Delete items?",
            f"Set the count of {len(items)} selected item record(s) to zero?\n\n"
            "The records stay in the save as empty slots, which the game shows "
            "grayed out. Save these changes, then use Purge Empty Slots to "
            "delete empty carried slots outright.",
        ):
            return
        changed = 0
        for item in items:
            entry = item.entries.get("ItemCount")
            if entry and int(self.pending.get(entry.offset, entry.value)) != 0:
                self.pending[entry.offset] = 0
                changed += 1
        self._refresh_values()
        self.status_var.set(f"Queued deletion of {changed} item record(s)")

    def _delete_items_event(self, _event: tk.Event | None = None) -> str:
        self._delete_items()
        return "break"

    def _structural_guard(self) -> bool:
        """Shared preconditions for edits that rewrite the save's structure."""
        if not self.document:
            return False
        if _run_in_background(self, lambda: _game_is_running()):
            messagebox.showerror(
                "Game is running", "Close The Last Caretaker before writing its save.")
            return False
        if not self._sync_document():
            return False
        if _unsaved_edits(self):
            messagebox.showwarning(
                "Unsaved edits",
                "Structural changes rewrite the whole save, which invalidates the "
                "pending edits.\n\nSave or discard them first.",
            )
            return False
        return True

    @_not_while_pumping
    def _purge_empty_slots(self) -> None:
        if not self._structural_guard():
            return
        include_world = self.purge_include_world.get()
        with _busy_ui(self, "Scanning for empty slots..."):
            try:
                empty = _run_in_background(
                    self, save_io.count_empty_slots, self.document,
                    player_only=not include_world)
            except Exception as exc:
                messagebox.showerror("Scan failed", str(exc))
                return
        if not empty:
            scope = "the save" if include_world else "the player inventory"
            self.status_var.set(f"No empty slots to purge in {scope}")
            return
        scope_text = (
            "every zero-count record in the save, including environment, "
            "container and world items"
            if include_world
            else "the player inventory"
        )
        if self.confirm_purge_var.get() and not messagebox.askyesno(
            "Purge empty slots?",
            f"Remove {empty} zero-count record(s) from {scope_text}?\n\n"
            "These are the grayed-out entries the game will not let you drop. "
            "The save is rewritten immediately. Use Create Backup Now first "
            "if you want an additional restore point.",
        ):
            return
        keep = max(1, self.keep_var.get())
        self._purge(lambda doc: save_io.purge_items(
            doc, zero_count_only=True, player_only=not include_world,
            make_backup=False, max_backups=keep))

    @_not_while_pumping
    def _remove_duplicate_stacks(
        self, *, confirm: bool = True, include_memory: bool = False,
    ) -> None:
        """Collapse carried items split across several redundant stacks."""
        if not self._structural_guard():
            return
        with _busy_ui(self, "Scanning for duplicate stacks..."):
            try:
                duplicates = _run_in_background(
                    self, save_io.find_duplicate_stacks, self.document,
                    include_memory=include_memory,
                )
                unpurgeable = 0 if duplicates else _run_in_background(
                    self, save_io.count_unpurgeable_duplicate_stacks, self.document,
                    include_memory=include_memory,
                )
            except Exception as exc:
                messagebox.showerror("Scan failed", str(exc))
                return
        if not duplicates:
            if unpurgeable:
                self.status_var.set(
                    f"{unpurgeable} duplicate carried stack(s) detected but "
                    "cannot be safely removed yet (a section of this save's "
                    "item data can't be fully read)"
                )
            else:
                self.status_var.set("No duplicate carried stacks found")
            return
        names = sorted({item.asset for item in duplicates}, key=str.casefold)
        preview = ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
        if confirm and not messagebox.askyesno(
            "Remove duplicate stacks?",
            f"Delete {len(duplicates)} redundant carried stack(s) covering "
            f"{len(names)} item(s): {preview}\n\n"
            "For each eligible item, the single largest carried stack is kept "
            "and every other stack is deleted outright (not merged into the "
            "kept stack). Ammo, Memory, and Weapon categories are protected; "
            "the three exact player-backpack equipment grenades are explicit "
            "cleanup exceptions. Weapon Attachments are never combined or "
            "deleted. "
            "Only items you are carrying are affected. The save is "
            "rewritten immediately. Use Create Backup Now first if you want "
            "an additional restore point.",
        ):
            return
        keep = max(1, self.keep_var.get())
        self._purge(lambda doc: save_io.purge_items(
            doc, duplicates, player_only=False, make_backup=False,
            max_backups=keep))

    @_not_while_pumping
    def _mark_blueprints_scanned(self) -> None:
        """Add Scanned records for every blueprint the save knows but hasn't scanned."""
        if not self._structural_guard():
            return
        try:
            status = _run_in_background(self, save_io.blueprint_scan_status, self.document)
        except Exception as exc:
            messagebox.showerror("Scan failed", str(exc))
            return
        missing = [name for name, scanned in status if not scanned]
        if not missing:
            self.status_var.set(
                f"All {len(status)} known blueprint(s) are already scanned" if status
                else "No blueprints found in this save")
            return
        if not messagebox.askyesno(
            "Mark blueprints scanned?",
            f"Add scan records for {len(missing)} unscanned blueprint(s):\n\n"
            + "\n".join(missing)
            + "\n\nThis inserts new records into the save's scan log and is "
            "experimental. A backup is created first. If the game does not "
            "unlock the build, or fails to load, restore that backup.",
        ):
            return
        keep = max(1, self.keep_var.get())
        with _busy_ui(self, "Adding blueprint scan records..."):
            try:
                added, backup = _run_in_background(
                    self, save_io.mark_blueprints_scanned, self.document, missing,
                    make_backup=True, max_backups=keep)
            except Exception as exc:
                messagebox.showerror("Mark scanned failed", str(exc))
                return
            self._load(self.document.path, confirm_discard=False)
        self.status_var.set(
            f"Marked {len(added)} blueprint(s) scanned"
            + (f"; backup: {backup.name}" if backup else ""))

    def _apply_repair_targets(self, values: dict[str, str],
                              asset_values: dict[str, str] | None = None) -> dict[str, int]:
        """Validate raw dialog text, store and persist it. Raises ValueError on bad input."""
        parsed: dict[str, int] = {}
        for key, _label, _default, _everywhere in save_io.REPAIR_CATEGORIES:
            text = str(values.get(key, "")).strip()
            try:
                number = int(text)
            except ValueError:
                raise ValueError(f"{' / '.join(self._repair_label(key))}: '{text}' is not a whole number") from None
            if not 1 <= number <= save_io.MAX_STACK_TARGET:
                raise ValueError(
                    f"{' / '.join(self._repair_label(key))}: must be between 1 and {save_io.MAX_STACK_TARGET}")
            parsed[key] = number
        if asset_values is not None:
            assets: dict[str, int] = {}
            for name, raw_text in asset_values.items():
                text = str(raw_text).strip()
                if not text:
                    continue
                try:
                    number = int(text)
                except ValueError:
                    raise ValueError(f"{name}: '{text}' is not a whole number") from None
                if not 1 <= number <= save_io.MAX_STACK_TARGET:
                    raise ValueError(f"{name}: must be between 1 and {save_io.MAX_STACK_TARGET}")
                assets[name] = number
            self.repair_asset_targets = save_io.normalize_asset_targets(assets)
        self.repair_targets = save_io.normalize_repair_targets(parsed)
        self._write_settings()
        return self.repair_targets

    def _capture_asset_targets(self) -> int:
        """Store the loaded save's current carried stack sizes as per-item targets."""
        if not self.document:
            return 0
        captured = save_io.asset_targets_from_document(self.document)
        self.repair_asset_targets = save_io.normalize_asset_targets(captured)
        self._write_settings()
        self.status_var.set(
            f"Captured {len(self.repair_asset_targets)} per-item stack size(s) from this save")
        return len(self.repair_asset_targets)

    def _clear_asset_targets(self) -> None:
        self.repair_asset_targets = {}
        self._write_settings()
        self.status_var.set("Cleared per-item stack sizes; categories apply again")

    @staticmethod
    def _repair_label(key: str) -> tuple[str, ...]:
        for row_key, label, _default, _everywhere in save_io.REPAIR_CATEGORIES:
            if row_key == key:
                return label
        return (key,)

    def _apply_deletion_items(self, names: list[str]) -> bool:
        previous = self.repair_delete_items
        self.repair_delete_items = save_io.normalize_deletion_items(names)
        if not self._write_settings():
            self.repair_delete_items = previous
            messagebox.showerror("Settings failed", "Could not save the item deletion list.")
            return False
        self.status_var.set(f"Saved {len(self.repair_delete_items)} item name(s) for deletion")
        return True

    def _configure_deletion_items(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Repair & Top Off - Delete Items")
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame, text="Select an inventory name in the drop-down to mark it for deletion.\n"
            "Saved names delete carried copies after Repair & Top Off saves successfully.\n"
            "World/catalog copies and weapon attachments are left alone. Nothing is deleted now.",
        ).pack(anchor="w", pady=(0, 8))
        names = sorted(
            {item.asset.strip() for item in self.document.items if item.asset.strip()}
            if self.document else set(), key=str.casefold,
        )
        marked = list(self.repair_delete_items)
        ttk.Label(frame, text="Filter (wildcards and -exclusions supported):").pack(anchor="w")
        filter_var = tk.StringVar()
        filter_entry = ttk.Entry(frame, textvariable=filter_var)
        filter_entry.pack(fill=tk.X, pady=(2, 6))
        item_var = tk.StringVar()
        combo = ttk.Combobox(frame, textvariable=item_var, values=names, state="readonly", width=65)
        combo.pack(fill=tk.X)
        ttk.Label(frame, text="Marked for deletion:").pack(anchor="w", pady=(10, 2))
        body = ttk.Frame(frame)
        body.pack(fill=tk.BOTH, expand=True)
        selected = tk.Listbox(body, selectmode=tk.EXTENDED, exportselection=False, height=12)
        selected.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=selected.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        selected.configure(yscrollcommand=scrollbar.set)

        def refresh() -> None:
            selected.delete(0, tk.END)
            for name in marked:
                selected.insert(tk.END, name)
            taken = {name.casefold() for name in marked}
            combo.configure(values=[
                name for name in names
                if name.casefold() not in taken and self._matches_filter(filter_var.get(), name)
            ])

        def add(_event=None) -> None:
            name = item_var.get()
            if name in names:
                marked[:] = save_io.normalize_deletion_items(marked + [name])
                refresh()
                item_var.set("")

        def remove() -> None:
            for index in reversed(selected.curselection()):
                del marked[index]
            refresh()

        def save() -> None:
            if self._apply_deletion_items(marked):
                dialog.destroy()

        combo.bind("<<ComboboxSelected>>", add)
        filter_var.trace_add("write", lambda *_: refresh())
        selected.bind("<Delete>", lambda _event: remove())
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="Remove from List", command=remove).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT, padx=4)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        refresh()
        filter_entry.focus_set()

    def _configure_repair_targets(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Repair & Top Off Stack Targets")
        dialog.transient(self)
        dialog.grab_set()
        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        guidance = ttk.Label(
            outer,
            text="Stack size Repair & Top Off sets for each category. Rows marked * also apply to\n"
                 "records that are not carried; other non-carried items use the Not Carried target.\n"
                 "Storage refill amount applies only with Refill Storage Tanks enabled, independent of item selection.\n"
                 "Rocket Resources apply only with Top Off Rocket Resources enabled and override other refill amounts.\n"
                 "Higher than default rocket amounts can prevent the rocket from functioning.\n"
                 "Per-item stack sizes at the bottom override item categories, not storage refill.",
            wraplength=560,
        )
        guidance.pack(fill=tk.X, pady=(0, 6))
        guidance.bind("<Configure>", lambda event: guidance.configure(wraplength=max(1, event.width)))
        body = ttk.Frame(outer)
        body.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(body, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        grid = ttk.Frame(canvas)
        grid.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=grid, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        variables: dict[str, tk.StringVar] = {}
        defaults = save_io.default_repair_targets()
        shown: set[tuple[str, ...]] = set()
        row = 0
        for key, label, default, everywhere in save_io.REPAIR_CATEGORIES:
            for depth in range(len(label) - 1):
                header = label[: depth + 1]
                if header not in shown:
                    shown.add(header)
                    ttk.Label(grid, text=header[-1], font=("TkDefaultFont", 9, "bold")).grid(
                        row=row, column=0, sticky="w", padx=(depth * 18, 0), pady=(6, 0))
                    row += 1
            text = label[-1] + (" *" if everywhere else "")
            ttk.Label(grid, text=text).grid(row=row, column=0, sticky="w", padx=((len(label) - 1) * 18, 12))
            var = tk.StringVar(value=str(self.repair_targets.get(key, default)))
            ttk.Entry(grid, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=1)
            ttk.Label(grid, text=f"default {default}", foreground="#666").grid(
                row=row, column=2, sticky="w", padx=(8, 0))
            variables[key] = var
            row += 1

        asset_vars: dict[str, tk.StringVar] = {}
        ttk.Label(grid, text="Per-Item Stack Sizes", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, sticky="w", pady=(10, 0))
        row += 1
        if self.repair_asset_targets:
            ttk.Label(
                grid,
                text="These win over the category above. Blank a value to drop the override.",
                foreground="#666",
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
            row += 1
            for name, value in self.repair_asset_targets.items():
                ttk.Label(grid, text=name).grid(row=row, column=0, sticky="w", padx=(18, 12))
                var = tk.StringVar(value=str(value))
                ttk.Entry(grid, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=1)
                asset_vars[name] = var
                row += 1
        else:
            ttk.Label(
                grid,
                text="None configured - use 'Capture From Save' to lock in today's stack sizes.",
                foreground="#666",
            ).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        def save() -> None:
            try:
                applied = self._apply_repair_targets(
                    {key: var.get() for key, var in variables.items()},
                    {name: var.get() for name, var in asset_vars.items()},
                )
            except ValueError as exc:
                messagebox.showerror("Invalid stack target", str(exc), parent=dialog)
                return
            changed = sum(1 for key, value in applied.items() if value != defaults[key])
            self.status_var.set(
                f"Saved Repair & Top Off stack targets ({changed} changed from default, "
                f"{len(self.repair_asset_targets)} per-item)")
            dialog.destroy()

        def reset() -> None:
            for key, var in variables.items():
                var.set(str(defaults[key]))

        def capture() -> None:
            if not self.document:
                messagebox.showinfo("No save loaded", "Load a save first.", parent=dialog)
                return
            count = self._capture_asset_targets()
            messagebox.showinfo(
                "Captured stack sizes",
                f"Stored {count} per-item stack size(s) from the loaded save.",
                parent=dialog,
            )
            dialog.destroy()
            self._configure_repair_targets()

        def clear_assets() -> None:
            if not self.repair_asset_targets:
                return
            if not messagebox.askyesno(
                "Clear per-item stack sizes?",
                f"Remove all {len(self.repair_asset_targets)} per-item stack size(s)?",
                parent=dialog,
            ):
                return
            self._clear_asset_targets()
            dialog.destroy()
            self._configure_repair_targets()

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(actions, text="Reset to Defaults", command=reset).pack(side=tk.LEFT)
        ttk.Button(actions, text="Capture From Save", command=capture).pack(side=tk.LEFT, padx=4)
        ttk.Button(actions, text="Clear Per-Item", command=clear_assets).pack(side=tk.LEFT)
        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT, padx=4)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.update_idletasks()
        width = max(grid.winfo_reqwidth() + scroll.winfo_reqwidth() + 30, 420)
        height = min(grid.winfo_reqheight() + 140, max(300, self.winfo_screenheight() - 160))
        dialog.geometry(f"{width}x{height}")

    def _toggle_player_lock(self, key: str) -> None:
        """Write the infinite health/energy lock state chosen by its checkbox."""
        var = self.player_lock_vars[key]
        locked = var.get()
        if getattr(self, "_pumping", False) is True:
            # A click delivered while another operation runs: undo the toggle.
            var.set(not locked)
            return
        spec = save_io.PLAYER_LOCKS[key]
        if not self._structural_guard():
            var.set(not locked)
            return
        values = spec["locked" if locked else "normal"]
        summary = ", ".join(f"{name} {value:g}" for name, value in values.items())
        if not messagebox.askyesno(
            f"{'Enable' if locked else 'Disable'} {spec['label']}?",
            f"{'Pin' if locked else 'Unpin'} the player's {spec['attribute']} survival attribute "
            f"({summary}) and save now?\n\nA backup is created first.",
        ):
            var.set(not locked)
            return
        keep = max(1, self.keep_var.get())
        with _busy_ui(self, f"{'Enabling' if locked else 'Disabling'} {spec['label']}..."):
            try:
                changed, backup = _run_in_background(
                    self, save_io.set_player_lock, self.document, key, locked,
                    make_backup=True, max_backups=keep)
            except Exception as exc:
                var.set(not locked)
                messagebox.showerror("Toggle failed", str(exc))
                return
            self._load(self.document.path, confirm_discard=False)
        self.status_var.set(
            f"{spec['label']} {'enabled' if locked else 'disabled'} ({changed} value(s) written)"
            + (f"; backup: {backup.name}" if backup else ""))

    @_not_while_pumping
    def _purge(self, action) -> None:
        with _busy_ui(self, "Rewriting save structure...") as progress:
            try:
                removed, backup, _report = _run_in_background(self, action, self.document)
            except Exception as exc:
                messagebox.showerror("Remove failed", str(exc))
                return
            if not removed:
                self.status_var.set("Nothing matched; the save was not modified")
                return
            progress("Reloading rewritten save...")
            path = self.document.path
            self._load(path, confirm_discard=False)
        self.status_var.set(
            f"Removed {removed} record(s).")

    def _edit_item_cell(self, event: tk.Event) -> None:
        iid = self.inventory_tree.identify_row(event.y)
        column = self.inventory_tree.identify_column(event.x)
        if not iid or iid not in self.item_rows:
            return
        fields = {4: "ItemCount", 5: "Health", 6: "SelectedVariation"}
        field = fields.get(int(column[1:]), "")
        if field and len(self.inventory_tree.selection()) > 1:
            self._set_item_field(field, field.casefold())
            return
        entry = self.item_rows[iid].entries.get(field)
        if entry:
            self._prompt_entry(entry)

    def _edit_scalar(self, _event: tk.Event | None = None) -> None:
        selection = self.scalar_tree.selection()
        if selection and selection[0] in self.scalar_rows:
            self._prompt_entry(self.scalar_rows[selection[0]])

    def _set_selected_scalars(self) -> None:
        entries = [self.scalar_rows[iid] for iid in self.scalar_tree.selection() if iid in self.scalar_rows]
        if not entries:
            return
        value = simpledialog.askstring("Set selected values", f"New value for {len(entries)} scalar(s):", parent=self)
        if value is None:
            return
        try:
            parsed = [(entry, save_io.parse_value(entry, value)) for entry in entries]
            for entry, new_value in parsed:
                self.pending[entry.offset] = new_value
            self._refresh_values()
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))

    @_not_while_pumping
    def _save(self, *, confirm: bool = True) -> bool:
        if not self.document:
            self.status_var.set("No pending edits")
            return False
        human_changes = dict(getattr(self, "human_pending", {}))
        attribute_changes = dict(getattr(self, "human_attribute_pending", {}))
        human_keys = human_changes.keys() | {key[0] for key in attribute_changes}
        if not self.pending and not human_keys:
            self.status_var.set("No pending edits")
            return True
        if _run_in_background(self, lambda: _game_is_running()):
            messagebox.showerror("Game is running", "Close The Last Caretaker before writing its save.")
            return False
        try:
            keep = max(1, self.keep_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid backup count", "Enter a whole number in Settings > Backups > Keep backups.")
            return False
        if human_keys:
            data = self.document.human_data
            humans = {human.key: human for human in data.humans}
            problems = [
                f"{key[1]}: " + (humans[key].edit_error if key in humans else "no longer in this save")
                for key in human_keys if key not in humans or humans[key].edit_error
            ]
            if data.error or problems:
                messagebox.showerror(
                    "Human data changed",
                    (data.error or "\n".join(problems[:10])) + "\n\nReload the save and queue the human edits again.",
                )
                return False
            if not messagebox.askyesno(
                "Write save?", self._save_prompt(humans, human_changes, keep, attribute_changes),
            ):
                return False
        elif confirm and self.confirm_save_var.get() and not messagebox.askyesno(
            "Write save?",
            f"Apply {len(self.pending)} edit(s) to:\n{self.document.path}\n\n"
            "Use Create Backup Now first if you want an additional restore point.",
        ):
            return False
        entries = {entry.offset: entry for entry in self.document.entries}
        edits = [(entries[offset], value) for offset, value in self.pending.items()]
        failure: Exception | None = None
        loaded = False
        changed = humans_changed = 0
        backup = None
        count = len(edits) + len(human_changes) + len(attribute_changes)
        with _busy_ui(self, f"Saving {count} change(s)...") as progress:
            try:
                changed, humans_changed, backup = _run_in_background(
                    self, save_io.save_changes, self.document, edits, human_changes,
                    human_attributes=attribute_changes, make_backup=bool(human_keys), max_backups=keep)
            except Exception as exc:
                failure = exc
            else:
                if changed or humans_changed:
                    progress("Reloading saved data...")
                    loaded = self._load(self.document.path, confirm_discard=False)
        if failure is not None:
            self.status_var.set("Save failed; pending edits are kept")
            messagebox.showerror("Save failed", str(failure))
            return False
        if not changed and not humans_changed:
            self.pending.clear()
            self.human_pending = {}
            self.human_attribute_pending = {}
            self._refresh_values()
            self._populate_humans()
            self.status_var.set("Nothing to save: the pending values already match the save")
            return True
        parts = []
        if changed:
            parts.append(f"{changed} edit(s)")
        if humans_changed:
            parts.append(f"professions for {humans_changed} human(s)")
        note = f" (backup: {backup.name})" if backup else ""
        if not loaded:
            # The file was written, so the queue is spent; the stale view must not be edited again.
            self.pending.clear()
            self.human_pending = {}
            self.human_attribute_pending = {}
            self.document.human_data.error = (
                "Changes were saved, but reload failed. Reload this save before editing again.")
            self._refresh_values()
            self._populate_humans()
            self._populate_backups()
            self.status_var.set(f"Saved {' and '.join(parts)}{note}, but reload failed. "
                                "Reload this save before editing again.")
            return False
        self.status_var.set(f"Saved {' and '.join(parts)}{note}.")
        return True

    def _save_prompt(self, humans: dict, human_changes: dict, keep: int, attribute_changes: dict) -> str:
        lines = []
        for key in list(human_changes)[:6]:
            human = humans[key]
            old = "; ".join(map(human_io.profession_label, human.professions)) or "No profession"
            new = human_io.profession_label(human_changes[key]) if human_changes[key] else "No profession"
            lines.append(f"  {human.social_id}: {old} -> {new}")
        if len(human_changes) > 6:
            lines.append(f"  ...and {len(human_changes) - 6} more")
        for (key, source, asset), value in list(attribute_changes.items())[:6]:
            site = humans[key].attribute_sets.get(source)
            old = site.values.get(asset) if site else None
            label = human_io.ATTRIBUTE_SOURCES.get(source, source)
            lines.append(f"  {key[1]}: {human_io.asset_label(asset)} / {label}: {old} -> {value}")
        if len(attribute_changes) > 6:
            lines.append(f"  ...and {len(attribute_changes) - 6} more stat field(s)")
        parts = []
        if self.pending:
            parts.append(f"{len(self.pending)} value edit(s)")
        if human_changes:
            parts.append(f"profession changes for {len(human_changes)} human(s)")
        if attribute_changes:
            parts.append(f"{len(attribute_changes)} raw stat field(s)")
        details = "Mutations, other traits, Social IDs and launch status are preserved.\n\n"
        if human_changes:
            details += (
                "Each profession change REPLACES all existing profession traits for that human. "
                "Matching pod current/simulated professions are updated together.\n\n"
            )
        if attribute_changes:
            details += (
                "Only the listed raw stat fields change. Roster and pod values are edited independently; "
                "growth points are not converted to in-game stats, units or age.\n\n"
            )
        else:
            details += "Attributes and growth stats are not changed.\n\n"
        details += "The game may recalculate stats and professions for humans still in pods.\n\n"
        return (
            f"Write {' and '.join(parts)} to:\n{self.document.path}\n\n"
            + "\n".join(lines) + "\n\n" + details + f"A backup is created first (keep {keep})."
        )

    @_not_while_pumping
    def _create_backup(self) -> None:
        if not self.document:
            return
        keep = max(1, self.keep_var.get())
        try:
            with _busy_ui(self, "Creating backup..."):
                path = _run_in_background(self, save_io.backup_save, self.document.path, keep)
            self._populate_backups()
            self.status_var.set(f"Created {path.name}")
        except Exception as exc:
            messagebox.showerror("Backup failed", str(exc))

    @_not_while_pumping
    def _restore_backup(self) -> None:
        if not self.document:
            return
        selection = self.backup_tree.selection()
        if not selection:
            return
        path = Path(self.backup_tree.set(selection[0], "path"))
        if not messagebox.askyesno("Restore backup?", f"Replace the active save with:\n{path}?"):
            return
        if _run_in_background(self, lambda: _game_is_running()):
            messagebox.showerror("Game is running", "Close The Last Caretaker before restoring a save.")
            return
        try:
            with _busy_ui(self, "Restoring backup..."):
                _run_in_background(self, save_io.restore_backup, self.document.path, path)
                self._load(
                    self.document.path,
                    confirm_discard=False,
                )
            self.status_var.set(f"Restored {path.name}")
        except Exception as exc:
            messagebox.showerror("Restore failed", str(exc))

    def _open_folder(self) -> None:
        if self.document:
            os.startfile(self.document.path.parent)

    def _select_all(self, tree: ttk.Treeview) -> str:
        tree.selection_set(tree.get_children())
        return "break"

    def _list_edge(self, tree: ttk.Treeview, *, first: bool, event: tk.Event) -> str:
        rows = list(tree.get_children())
        if not rows:
            return "break"
        target = rows[0] if first else rows[-1]
        if event.state & 0x0001:
            focus = tree.focus()
            anchor_index = rows.index(focus) if focus in rows else (len(rows) - 1 if first else 0)
            target_index = 0 if first else len(rows) - 1
            low, high = sorted((anchor_index, target_index))
            tree.selection_set(rows[low:high + 1])
        else:
            tree.selection_set(target)
        tree.focus(target)
        tree.see(target)
        return "break"

    def _sort_tree(self, tree: ttk.Treeview, column: str) -> None:
        key = (id(tree), column)
        descending = not self.sort_desc.get(key, False)
        self.sort_desc[key] = descending
        self.sort_state[id(tree)] = (column, descending)
        self._sort_rows(tree, column, descending)

    def _sort_rows(self, tree: ttk.Treeview, column: str, descending: bool) -> None:
        def sortable(iid: str) -> tuple[int, object]:
            value = tree.set(iid, column)
            try:
                return (0, float(value.replace(",", "")))
            except ValueError:
                return (1, value.casefold())
        rows = sorted(tree.get_children(), key=sortable, reverse=descending)
        # Indexed moves walk the sibling list each time (quadratic on large saves).
        tree.set_children("", *rows)

    def _apply_saved_sort(self, tree: ttk.Treeview) -> None:
        saved = self.sort_state.get(id(tree))
        if saved:
            self._sort_rows(tree, saved[0], saved[1])

    def _update_pending(self) -> None:
        edits = len(self.pending)
        professions = len(getattr(self, "human_pending", {}))
        stats = len(getattr(self, "human_attribute_pending", {}))
        parts = []
        if edits:
            parts.append(f"{edits} pending edit(s)")
        if professions:
            parts.append(f"{professions} profession change(s)")
        if stats:
            parts.append(f"{stats} human stat field(s)")
        self.pending_var.set(" | ".join(parts) or "No pending edits")
        suffix = " *" if parts else ""
        name = self.document.path.name if self.document else "No save"
        self.title(f"The Last Caretaker Save Editor - {name}{suffix}")

    def _current_settings(self) -> dict:
        return {
            "last_save": str(self.document.path) if self.document else "",
            "backup_keep": max(1, self.keep_var.get()),
            "confirm_save": self.confirm_save_var.get(),
            "confirm_purge": self.confirm_purge_var.get(),
            "confirm_repair": self.confirm_repair_var.get(),
            "confirm_delete": self.confirm_delete_var.get(),
            "repair_combine": self.repair_combine_var.get(),
            "auto_load_latest_save": self.auto_load_latest_var.get(),
            "repair_on_open": self.repair_on_open_var.get(),
            "show_empty_items": self.show_empty_items.get(),
            "purge_include_world": self.purge_include_world.get(),
            "map_layers": {key: var.get() for key, var in self.map_view.layer_vars.items()},
            "refill_storage_tanks": self.refill_storage_var.get(),
            "refill_rocket_resources": self.refill_rocket_var.get(),
            "repair_stack_targets": save_io.normalize_repair_targets(self.repair_targets),
            "repair_asset_targets": save_io.normalize_asset_targets(self.repair_asset_targets),
            "repair_delete_items": save_io.normalize_deletion_items(self.repair_delete_items),
            "geometry": self.geometry(),
        }

    def _write_settings(self) -> bool:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(self._current_settings(), indent=2), encoding="utf-8")
        except OSError:
            return False
        return True

    def _on_close(self) -> None:
        if _is_busy(self):
            self._confirm_quit_while_busy()
            return
        if _unsaved_edits(self) and not messagebox.askyesno("Discard edits?", "Close and discard pending edits?"):
            return
        self._write_settings()
        self.destroy()

    def _confirm_quit_while_busy(self) -> None:
        if getattr(self, "_quit_prompt_open", False) is True:
            return
        self._quit_prompt_open = True
        try:
            leave = messagebox.askyesno(
                "Operation in progress",
                "The editor is still working on the save.\n\n"
                "Quitting now abandons that operation. The save file is written to a "
                "temporary file and swapped in only when complete, so it is never left "
                "half-written, but a backup that is still being copied may be incomplete "
                "and any remaining steps will not run.\n\n"
                "Quit anyway?",
                icon="warning",
                default="no",
            )
        finally:
            self._quit_prompt_open = False
        if leave:
            self._quit_now()

    def _quit_now(self) -> None:
        """Leave at once, without waiting for the worker thread to finish."""
        self._write_settings()
        try:
            self.destroy()
        except tk.TclError:
            pass
        os._exit(0)


def main() -> int:
    _enable_windows_dpi_awareness()
    app = LastCaretakerEditor()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
