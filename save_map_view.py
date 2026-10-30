"""Native saved-position map for the editor's existing ttk interface."""
from __future__ import annotations

import math
import tkinter as tk
from collections import Counter, defaultdict
from tkinter import ttk
from typing import Callable

from voyage_map import LAYERS, LAYER_NAMES, MapCamera, MapData, MapPoint


class SaveMapView(ttk.Frame):
    def __init__(self, parent, *, reload_map: Callable[[], None],
                 matches_filter: Callable[[str, str], bool],
                 move_selected: Callable[[MapPoint], None] | None = None,
                 layer_settings: dict[str, bool] | None = None,
                 on_layers_changed: Callable[[], None] | None = None):
        super().__init__(parent)
        self.matches_filter = matches_filter
        self.move_selected_callback = move_selected
        self.on_layers_changed = on_layers_changed
        self.data: MapData | None = None
        self.visible: list[MapPoint] = []
        self.camera = MapCamera()
        self.groups: dict[str, list[MapPoint]] = {}
        self.selected_group: list[MapPoint] = []
        self.selected: MapPoint | None = None
        self._draw_job: str | None = None
        self._fit_pending = False
        self._drag_origin: tuple[float, float] | None = None
        self._drag_last: tuple[float, float] | None = None
        self.query = tk.StringVar()
        self.world_var = tk.StringVar()
        self.status = tk.StringVar(value="Load a save, then open Map to read saved positions.")
        self.layer_vars: dict[str, tk.BooleanVar] = {}
        self.layer_checks: dict[str, ttk.Checkbutton] = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        filters = ttk.Frame(self)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(filters, text="World:").pack(side=tk.LEFT)
        self.world_combo = ttk.Combobox(
            filters, textvariable=self.world_var, state="readonly", width=18,
        )
        self.world_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.world_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filter(fit=True))
        ttk.Label(filters, text="Find:").pack(side=tk.LEFT)
        entry = ttk.Entry(filters, textvariable=self.query)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        entry.bind("<Return>", lambda _e: self.apply_filter(fit=True))
        ttk.Button(filters, text="Go", width=4,
                   command=lambda: self.apply_filter(fit=True)).pack(side=tk.LEFT)
        ttk.Button(filters, text="Clear", command=self.clear_filter).pack(side=tk.LEFT, padx=4)
        toolbar = ttk.Frame(self)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        for text, command in (
            ("Fit visible", self.fit_visible), ("Center on player", self.center_player),
            ("Zoom +", lambda: self.zoom(1.5)), ("Zoom -", lambda: self.zoom(1 / 1.5)),
        ):
            ttk.Button(toolbar, text=text, command=command).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Refresh map", command=reload_map).pack(side=tk.LEFT, padx=4)
        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, background="#eef3f6", highlightthickness=1,
                                highlightbackground="#a7b5bf", takefocus=True)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", self._expand)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self.zoom(1.3, e.x, e.y))
        self.canvas.bind("<Button-5>", lambda e: self.zoom(1 / 1.3, e.x, e.y))
        self.canvas.bind("<plus>", lambda _e: self.zoom(1.5))
        self.canvas.bind("<minus>", lambda _e: self.zoom(1 / 1.5))
        for key, dx, dy in (("Left", 60, 0), ("Right", -60, 0),
                            ("Up", 0, 60), ("Down", 0, -60)):
            self.canvas.bind(f"<{key}>", lambda _e, x=dx, y=dy: self._pan(x, y))
        sidebar = ttk.Frame(body, padding=(12, 0, 0, 0), width=260)
        sidebar.grid(row=0, column=1, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(0, weight=1)
        self.inspector = ttk.Notebook(sidebar)
        self.inspector.grid(row=0, column=0, sticky="nsew")
        self.layers_tab = ttk.Frame(self.inspector, padding=4)
        self.details_tab = ttk.Frame(self.inspector, padding=4)
        self.inspector.add(self.layers_tab, text="Layers")
        self.inspector.add(self.details_tab, text="Object details")
        self.layers_tab.rowconfigure(0, weight=1)
        self.layers_tab.columnconfigure(0, weight=1)
        self.layer_canvas = tk.Canvas(self.layers_tab, width=300, height=1, highlightthickness=0)
        self.layer_canvas.grid(row=0, column=0, sticky="nsew")
        layer_scroll = ttk.Scrollbar(self.layers_tab, command=self.layer_canvas.yview)
        layer_scroll.grid(row=0, column=1, sticky="ns")
        self.layer_canvas.configure(yscrollcommand=layer_scroll.set)
        layers = ttk.Frame(self.layer_canvas)
        layer_window = self.layer_canvas.create_window(0, 0, window=layers, anchor=tk.NW)
        layers.bind("<Configure>", lambda _e: self.layer_canvas.configure(
            scrollregion=self.layer_canvas.bbox("all")))
        self.layer_canvas.bind("<Configure>", lambda e: self.layer_canvas.itemconfigure(
            layer_window, width=e.width))
        self.layer_canvas.bind("<MouseWheel>", self._scroll_layers)
        for key, label, color, enabled in LAYERS:
            row = ttk.Frame(layers)
            row.pack(fill=tk.X)
            dot = tk.Label(row, text="\u25cf", foreground=color)
            dot.pack(side=tk.LEFT)
            var = tk.BooleanVar(value=bool((layer_settings or {}).get(key, enabled)))
            check = ttk.Checkbutton(row, text=label, variable=var, command=self._layers_changed)
            check.pack(side=tk.LEFT)
            for widget in (row, dot, check):
                widget.bind("<MouseWheel>", self._scroll_layers)
            self.layer_vars[key], self.layer_checks[key] = var, check
        self.details_tab.columnconfigure(0, weight=1)
        self.details_tab.rowconfigure(4, weight=1)
        ttk.Label(self.details_tab, text="Objects at selected marker").grid(row=0, column=0, sticky="w")
        selection_frame = ttk.Frame(self.details_tab)
        selection_frame.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        selection_frame.columnconfigure(0, weight=1)
        self.selection_list = tk.Listbox(selection_frame, height=3, width=27, exportselection=False)
        self.selection_list.grid(row=0, column=0, sticky="ew")
        bar = ttk.Scrollbar(selection_frame, command=self.selection_list.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.selection_list.configure(yscrollcommand=bar.set)
        self.selection_list.bind("<<ListboxSelect>>", self._select_list)
        self.move_button = ttk.Button(
            self.details_tab, text="Move selected to player (1 m north)",
            command=self._move_selected, state=tk.DISABLED,
        )
        self.move_button.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        details_heading = ttk.Frame(self.details_tab)
        details_heading.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(details_heading, text="Saved object details").pack(side=tk.LEFT)
        ttk.Button(details_heading, text="Copy", command=self.copy_details).pack(side=tk.RIGHT)
        details_frame = ttk.Frame(self.details_tab)
        details_frame.grid(row=4, column=0, sticky="nsew")
        details_frame.columnconfigure(0, weight=1)
        details_frame.rowconfigure(0, weight=1)
        self.details = tk.Text(details_frame, width=27, height=1, wrap=tk.WORD, state=tk.DISABLED,
                               relief=tk.FLAT, background="#ffffff", font="TkDefaultFont")
        self.details.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(details_frame, command=self.details.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=detail_scroll.set)
        status_label = ttk.Label(self, textvariable=self.status, wraplength=950)
        status_label.grid(row=3, column=0, sticky="ew", pady=(6, 2))
        help_label = ttk.Label(
            self, text="Saved-position map: no terrain, collision data or routes. "
                       "Move is limited to loose human pods/eggs and saves a backup. "
                       "North is up (\u2212Y); east is right (+X). Coordinates are meters. "
                       "Drag to pan; wheel to zoom; double-click a cluster to expand.",
            wraplength=950,
        )
        help_label.grid(row=4, column=0, sticky="ew")
        for label in (status_label, help_label):
            label.bind("<Configure>", lambda e, widget=label: widget.configure(
                wraplength=max(1, e.width)))
        self.reset()

    def _layers_changed(self) -> None:
        self.apply_filter()
        if self.on_layers_changed is not None:
            self.on_layers_changed()

    def _scroll_layers(self, event) -> str:
        self.layer_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def reset(self, message: str = "Load a save, then open Map to read saved positions.") -> None:
        self.data = None
        self.visible = []
        self.groups.clear()
        self._select_group([])
        self.status.set(message)
        self.schedule_draw()

    def set_data(self, data: MapData) -> None:
        old_world = self.world_var.get()
        self.data = data
        self.world_combo.configure(values=data.worlds)
        preferred = next((point.world for point in data.points if point.layer == "player"), "")
        self.world_var.set(old_world if old_world in data.worlds else preferred or next(iter(data.worlds), ""))
        self._select_group([])
        self.apply_filter(fit=True)

    def clear_filter(self) -> None:
        self.query.set("")
        self.apply_filter(fit=True)

    def apply_filter(self, *, fit: bool = False) -> None:
        if self.data is None:
            return
        world = self.world_var.get()
        world_points = [point for point in self.data.points if point.world == world]
        counts = Counter(point.layer for point in world_points)
        for key, label, _color, _enabled in LAYERS:
            self.layer_checks[key].configure(text=f"{label} ({counts[key]:,})")
        enabled = {key for key, var in self.layer_vars.items() if var.get()}
        query = self.query.get().strip()
        self.visible = [point for point in world_points
                        if point.layer in enabled and self.matches_filter(query, point.searchable)]
        visible_keys = {point.key for point in self.visible}
        remaining = [point for point in self.selected_group if point.key in visible_keys]
        if remaining != self.selected_group:
            self._select_group(remaining)
        warning = f" | {len(self.data.warnings):,} pod state(s) unavailable" if self.data.warnings else ""
        notice = "".join(f" | {text}" for text in self.data.notices)
        self.status.set(
            f"{len(self.visible):,} matching objects / {len(world_points):,} mapped in this world"
            f" | {self.data.skipped_positions:,} actor(s) without supported positions{warning}{notice}")
        if fit:
            self.fit_visible()
        else:
            self.schedule_draw()

    def fit_visible(self) -> None:
        self.camera.fit(self._core_positions([point.position for point in self.visible]),
                        self.canvas.winfo_width(), self.canvas.winfo_height())
        self._fit_pending = self.canvas.winfo_width() < 100
        self.schedule_draw()

    @staticmethod
    def _core_positions(positions: list) -> list:
        """Drop the few far outliers (such as North Bastion) that would shrink everything else."""
        if len(positions) < 20:
            return positions
        bounds = []
        for axis in (0, 1):
            values = sorted(position[axis] for position in positions)
            low, high = values[len(values) // 50], values[-1 - len(values) // 50]
            margin = max(high - low, 1) * 0.25
            bounds.append((low - margin, high + margin))
        core = [position for position in positions
                if all(low <= position[axis] <= high for axis, (low, high) in enumerate(bounds))]
        return core or positions

    def center_player(self) -> None:
        player = self.data.player(self.world_var.get()) if self.data is not None else None
        if player is not None:
            self.camera.x, self.camera.y = player.position[:2]
            self.schedule_draw()
        elif self.data is not None:
            self.status.set("No unique saved player position is available in this world.")

    def zoom(self, factor: float, x: float | None = None, y: float | None = None) -> str:
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.camera.zoom(factor, width / 2 if x is None else x,
                         height / 2 if y is None else y, width, height)
        self.schedule_draw()
        return "break"

    def _wheel(self, event) -> str:
        return self.zoom(1.3 if event.delta > 0 else 1 / 1.3, event.x, event.y)

    def _pan(self, dx: float, dy: float) -> str:
        self.camera.pan(dx, dy)
        self.schedule_draw()
        return "break"

    def _resize(self, _event=None) -> None:
        if self._fit_pending and self.canvas.winfo_width() >= 100:
            self.fit_visible()
        else:
            self.schedule_draw()

    def schedule_draw(self) -> None:
        if self._draw_job is None:
            self._draw_job = self.after_idle(self.draw)

    def destroy(self) -> None:
        if self._draw_job is not None:
            self.after_cancel(self._draw_job)
            self._draw_job = None
        super().destroy()

    def draw(self) -> None:
        self._draw_job = None
        canvas = self.canvas
        canvas.delete("all")
        self.groups.clear()
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 10 or height < 10:
            return
        if self.data is None or not self.visible:
            message = ("Open this tab with a save loaded to build the map."
                       if self.data is None else "No matching objects.\nEnable a layer or clear Find.")
            canvas.create_text(width / 2, height / 2, text=message, fill="#344b5c",
                               font="TkDefaultFont", justify=tk.CENTER)
            return
        self._draw_grid(width, height)
        buckets: dict[tuple[int, int], list[MapPoint]] = defaultdict(list)
        for point in self.visible:
            x, y = self.camera.screen(*point.position[:2], width, height)
            if -20 <= x <= width + 20 and -20 <= y <= height + 20:
                buckets[math.floor(x / 18), math.floor(y / 18)].append(point)
        colors = {key: color for key, _label, color, _enabled in LAYERS}
        priority = {key: index for index, key in enumerate(
            ("humans", "player", "locations", "pods", "rockets", "checkpoints", "containers", "other"))}
        labels: list[tuple[float, float, str, str]] = []
        place_labels: list[tuple[float, float, str, str]] = []
        obstacles: list[tuple[float, float, float, float]] = []
        for group in buckets.values():
            point = min(group, key=lambda member: priority[member.layer])
            x = sum(p.position[0] for p in group) / len(group)
            y = sum(p.position[1] for p in group) / len(group)
            x, y = self.camera.screen(x, y, width, height)
            tag = f"marker:{len(self.groups)}"
            self.groups[tag] = group
            color = colors[point.layer]
            radius = 11 if len(group) > 1 else (7 if point.layer in ("player", "humans") else 4)
            selected = self.selected in group
            outline = "#142f43" if selected else "#ffffff"
            obstacles.append((x - radius - 3, y - radius - 3, x + radius + 3, y + radius + 3))
            if point.layer == "player" and len(group) == 1:
                canvas.create_polygon(x, y - 9, x + 8, y, x, y + 9, x - 8, y,
                                      fill=color, outline=outline, width=2, tags=(tag,))
            elif point.layer == "locations" and len(group) == 1:
                canvas.create_rectangle(x - 5, y - 5, x + 5, y + 5, fill=color, outline=outline,
                                        width=3 if selected else 1, tags=(tag,))
            else:
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                                   fill=color, outline=outline, width=3 if selected else 1, tags=(tag,))
            if len(group) > 1:
                canvas.create_text(x, y, text=str(len(group)), fill="#ffffff",
                                   font=("TkDefaultFont", 9, "bold"), tags=(tag,))
            elif point.layer in ("player", "humans"):
                labels.append((x, y, point.social_id[:8] or "Player", tag))
            elif point.layer == "locations":
                place_labels.append((x, y, point.label, tag))
        for x, y, text, tag in labels + place_labels:
            for dx, dy in ((12, -18), (12, 18), (-12, -18), (-12, 18),
                           (12, -38), (12, 38), (-12, -38), (-12, 38)):
                item = canvas.create_text(
                    x + dx, y + dy, text=text, fill="#173c55",
                    anchor=tk.W if dx > 0 else tk.E, font="TkDefaultFont", tags=(tag, "marker-label"))
                box = canvas.bbox(item)
                if (box is not None and 4 <= box[0] and 4 <= box[1]
                        and box[2] <= width - 4 and box[3] <= height - 4
                        and not any(box[0] < other[2] and box[2] > other[0]
                                    and box[1] < other[3] and box[3] > other[1]
                                    for other in obstacles)):
                    obstacles.append(box)
                    break
                canvas.delete(item)
        canvas.create_text(width - 10, height - 12, text="East, +X (m)", anchor=tk.E, fill="#3c5365")
        canvas.create_text(12, 12, text="\u25b2 North, \u2212Y (m)", anchor=tk.W, fill="#3c5365")

    def _draw_grid(self, width: int, height: int) -> None:
        target = 100 / self.camera.scale
        power = 10 ** math.floor(math.log10(target))
        step = next(multiplier * power for multiplier in (1, 2, 5, 10)
                    if multiplier * power >= target)
        left, top = self.camera.world(0, 0, width, height)
        right, bottom = self.camera.world(width, height, width, height)
        for i in range(math.ceil(left / step), math.floor(right / step) + 1):
            value = i * step
            x, _ = self.camera.screen(value, 0, width, height)
            self.canvas.create_line(x, 0, x, height, fill="#cbd7df")
            self.canvas.create_text(x + 3, height - 28, text=f"{value:,.0f}", anchor=tk.W,
                                    fill="#455e70", font="TkDefaultFont")
        for i in range(math.ceil(top / step), math.floor(bottom / step) + 1):
            value = i * step
            _, y = self.camera.screen(0, value, width, height)
            self.canvas.create_line(0, y, width, y, fill="#cbd7df")
            self.canvas.create_text(6, y - 3, text=f"{value:,.0f}", anchor=tk.SW,
                                    fill="#455e70", font="TkDefaultFont")

    def _hit(self, x: float, y: float) -> list[MapPoint]:
        for item in reversed(self.canvas.find_overlapping(x - 4, y - 4, x + 4, y + 4)):
            for tag in self.canvas.gettags(item):
                if tag in self.groups:
                    return self.groups[tag]
        return []

    def _press(self, event) -> None:
        self.canvas.focus_set()
        self._drag_origin = self._drag_last = (event.x, event.y)

    def _drag(self, event) -> None:
        if self._drag_last is not None:
            self.camera.pan(event.x - self._drag_last[0], event.y - self._drag_last[1])
            self._drag_last = (event.x, event.y)
            self.schedule_draw()

    def _release(self, event) -> None:
        if self._drag_origin is not None and math.dist(self._drag_origin, (event.x, event.y)) <= 4:
            self._select_group(self._hit(event.x, event.y))
        self._drag_origin = self._drag_last = None

    def _expand(self, event) -> str:
        group = self._hit(event.x, event.y)
        if group:
            self._select_group(group)
            self.camera.fit([p.position for p in group],
                            self.canvas.winfo_width(), self.canvas.winfo_height())
            self.schedule_draw()
        return "break"

    def _select_group(self, points: list[MapPoint]) -> None:
        self.selected_group = sorted(points, key=lambda p: (p.label, p.actor))
        self.selection_list.delete(0, tk.END)
        for point in self.selected_group:
            self.selection_list.insert(tk.END, point.label)
        self.selected = None
        self.move_button.configure(state=tk.DISABLED)
        if points:
            self.selection_list.selection_set(0)
            self.inspector.select(self.details_tab)
            self._select_list()
        else:
            self.inspector.select(self.layers_tab)
            self._set_details("Click a marker to see its saved coordinates and identity.\n\n"
                              "A numbered marker contains nearby objects; select an entry above "
                              "or double-click it to zoom in.")
        self.schedule_draw()

    def _select_list(self, _event=None) -> None:
        selection = self.selection_list.curselection()
        if not selection or self.data is None:
            return
        point = self.selected_group[selection[0]]
        self.selected = point
        movable = (self.move_selected_callback is not None
                   and point.actor_offset is not None and not point.move_block_reason)
        self.move_button.configure(state=tk.NORMAL if movable else tk.DISABLED)
        distance = self.data.distance(point)
        lines = [point.label,
                 "X / Y / Z (m): " + ", ".join(f"{value:,.1f}" for value in point.position),
                 f"3D distance from saved player: {distance:,.1f} m" if distance is not None
                 else "3D distance: no unique saved player position"]
        if point.social_id:
            lines.append(f"Social ID: {point.social_id}")
        if point.profession:
            lines.append(f"Saved profession: {point.profession}")
        lines.extend(point.details)
        lines.append(
            "Move status: " + (point.move_block_reason or
                               "Eligible: loose human pod/seed; a backup is created before saving.")
        )
        lines.extend((f"Layer: {LAYER_NAMES[point.layer]}", f"World: {point.world}",
                      f"Actor: {point.actor}"))
        self._set_details("\n".join(lines))
        self.schedule_draw()

    def _move_selected(self) -> None:
        if (self.selected is not None and self.move_selected_callback is not None
                and not self.selected.move_block_reason):
            self.move_selected_callback(self.selected)

    def _set_details(self, text: str) -> None:
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", text)
        self.details.configure(state=tk.DISABLED)

    def copy_details(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.details.get("1.0", "end-1c"))
