# The Last Caretaker Save Editor

A Windows GUI save editor for **The Last Caretaker**.

## Run

Close the game, then double-click `run.bat`.

The editor keeps machine-specific preferences in `editor_settings.json`
beside the application. This file is created locally and is intentionally
excluded from source control; it can contain a path to your save files.

## Download the Windows executable

Download the executable matching your Windows architecture from the latest
[GitHub release](https://github.com/matziq/the-last-caretaker-save-editor/releases):
`TheLastCaretakerSaveEditor-Win32.exe` is for 32-bit x86 Windows, and
`TheLastCaretakerSaveEditor-Win64.exe` is for 64-bit x64 Windows. Both are
self-contained; Python and a separate Oodle decoder installation are not
required. On first run, settings are stored in
`%LOCALAPPDATA%\The Last Caretaker Save Editor\editor_settings.json`.
Close the game before editing saves.

Releases are built automatically for version tags (`v*`) using the workflow in
`.github/workflows/release.yml`.

Newer saves use Oodle/Kraken compression. Install the decoder once with the
same Python interpreter that `run.bat` selects:

```powershell
python -m pip install -r requirements.txt
```

The dependency is `pyooz` (imported as `ooz`), an open-source decoder with
Windows wheels for Python 3.8+. No game DLLs are copied or required. Older
zlib saves remain readable even when the optional decoder is not installed.

At launch, the editor automatically finds saves in the current Windows
account's standard game-save folder:

`%LOCALAPPDATA%\Voyage\Saved\SaveGames\<SteamId>\<SteamId>_<slot>.sav`

With **Settings > Auto Load Latest Save** enabled (the default), it loads the
most recently modified save it finds. Disable this option to start without
loading a save; discovery still fills the save selector. The **Save file on this
PC** selector shows the full path, and **Browse...** opens this PC's standard
save folder so you can select a different save. Use **Refresh** to rescan.
No Windows username or save path is bundled with the application. You can also
browse to any compatible `.sav` manually.

## Features

- **Settings** centralizes all checkboxes and options: startup behavior,
  warnings, Repair & Top Off stack targets and deletion rules, inventory display,
  Purge scope, backup retention, player locks, and map layers. Preferences persist
  when changed; player locks reflect and edit the loaded save rather than being
  global defaults.
- **Repair and Top Off On Open** in Settings is off by default. When enabled,
  successfully opening a save at launch, choosing one in the selector, using
  **Browse...**, or manually reloading runs Repair & Top Off and saves immediately.
  It repairs all carried items regardless of the previous selection and uses the
  configured stack targets, storage refill, deletion list, and Combine option.
  The ordinary repair confirmation is skipped; configured-deletion confirmations
  still follow **Warn before Delete**. Failed/canceled loads do not run it, and
  internal reloads after writes do not trigger it again. The game must be closed.
  If the first save fails, configured deletion and combining do not run.
  At startup, the existing startup backup is created before automatic repair.
- Settings **Warn before saving**, **Warn before Purge**, **Warn before Repair & Top Off**, and
  **Warn before Delete** checkboxes (Delete covers Delete Selected and the
  configured Delete Items step of Repair & Top Off; declining the latter skips
  only the deletion) let you disable those confirmations independently. All default to
  enabled and persist in editor settings. Error messages, game-running checks,
  stale-save checks, and unsaved-edit safeguards remain active.
- **Progress**: edit the caretaker level, experience, level-cap experience,
  security level, player health values, collected pods and rockets, traveled
  distance, world time, and playtime when present. Settings **Infinite Health** /
  **Infinite Energy** checkboxes toggle trainer-style locks — see
  [Infinite health and energy](#infinite-health-and-energy-trainer-locks).
- **Mark Blueprints Scanned** (experimental): adds scan records for blueprints
  the save knows about but you haven't scanned — see
  [Blueprints and scanning](#blueprints-and-scanning-experimental).
- **Humans**: browse grown humans by Social ID, change professions, edit
  saved raw roster/pod stats, and see committee job coverage. Includes
  all **41 v6 professions** and **40 required roles across ten committees**.
  Queue profession and stat changes for as many humans as you like; **Save Changes**
  writes them all at once with a backup; see
  [Humans and committee professions](#humans-and-committee-professions).
- **Container Resources**: edit stored Diesel, Petrol, Electricity (battery
  charge), water, gases, and other serialized resource amounts using separate
  full-row text filter; double-click a row or
  use **Edit Selected...** (multi-select supported), then **Save Changes**.
  These are the untagged floating-point values in the saved
  `Resources` map, keyed by `EModuleResourceType`, not inventory stack counts.
  The resource key is authoritative: a Petrol-named container actor can
  actually hold Diesel, including a ship's installed fuel tank. The tab
  hides any actor containing `_C_UAID_` (preplaced world entities). It includes
  other containers, vehicle tanks, generators, and stored modules
  inside inventory, including zero amounts; only existing entries are edited.
  Inventory copies use the same mirrored-write protection as other edits.
  Resource values also appear in **All Scalars** as `Resources.Diesel`,
  `Resources.Petrol`, etc. Negative or non-finite amounts are rejected.
  Maximum capacities and units have not been identified in the save, so the
  editor displays raw stored amounts without guessing liters, percentages,
  or refill limits. Unknown `Custom1`/`Custom2` keys keep their saved names.
  **Health (raw)** and **Extra Damage (raw)** show saved condition fields
  when they can be associated unambiguously with the same actor or item.
  Double-click those columns or use **Edit Health... / Edit Extra Damage...**.
  They are not percentages: observed undamaged storage actors have Health
  **200**, but maximum health and the game's percentage formula are not
  confirmed. No automatic percentage conversion is performed.
  **Repair & Top Off**, with **Refill Storage Tanks** enabled (default),
  sets **Stored Amount to 50000**, **Health to 10000**, and **ExtraDamage to
  10000** for all visible Container Resources, including generators,
  empty entries, and amounts already above the target. The exception is
  infinite **Electricity** belonging to `BP_FirstPersonCharacter_New_C1` in
  **Player Backpack**: its amount and associated condition fields are left
  untouched. Actors containing `NPC` or `_C_UAID_` remain hidden and untouched,
  except the Functional Rocket described below. Scope is
  independent of inventory selection and text filters; actor numeric suffixes
  no longer restrict it. Missing or ambiguous condition fields are not created
  or guessed. Shared fields are updated only once.
  Change the three **Storage** targets in **Stack Targets...**; they persist
  with other targets. The checkbox also persists. Overfilling is allowed;
  values are not capacity-clamped. ExtraDamage is written exactly as requested;
  its gameplay effect is unconfirmed and it is not treated as a health percentage.
  Storage refill is queued with the initial repair and saved before cleanup
  or Combine; a failed save stops those later operations.
  Consumable battery items
  without a saved `Resources` entry have no separate editable charge here.
- **All Scalars**: editable serialized scalar values with one full-row text filter.
- **Inventory**: saved item, material, and equipment records with separate
  full-row text filter. Inventory, Container Resources, and All Scalars each
  have one filter searching names, types, values, locations, actors, and other
  record data. Searches are case-insensitive; all space-separated terms must
  match somewhere in the row. Prefix a term with `-` to exclude matching rows.
  Wildcards `*` (any characters) and `?` (one character) work inside terms,
  including exclusions. For example, `electricity battery -NPC` requires
  Electricity and Battery anywhere in the row but excludes NPC, while
  `electric* bat?ery -NPC*` uses wildcards. Enter or **Go** applies the filter;
  **Clear** removes it. Records are classified
  as Player Inventory, Container/Crate, Environment, or Other. Edit stack count,
  health/durability, and variation values that
  exist in the save. Multi-select records or press `Ctrl+A` to set counts in
  bulk. **Add Item** chooses from the complete player item catalog and
  increases its owned count; **Delete Selected** or the `Delete` key sets one
  or more selected records to zero after confirmation.
  Zero-count catalog entries are hidden by default and can be shown again.
  **Purge Empty Slots** deletes zero-count carried records outright — see
  [Deleting items](#deleting-items).
  Maintenance buttons are ordered left to right: green **Repair & Top Off**,
  gray **Remove Duplicate Stacks**, red **Purge Empty Slots**.
  **Repair & Top Off** refills and repairs the given records — selection
  first (any location), otherwise every carried item (backpack, holster,
  equipped, or player-created crate). It follows the item stack rules below. Any selected record
  the player is **not** carrying (environment, container, catalog, module
  mirror) is compacted to a stack of **1** instead of being topped off,
  except Memory items (**10**), Ammo (**2500**), Big Petrol Grenades
  (**2500**), other grenades (**500**), and weapon attachments (**10**),
  which use those exact counts whether carried or not, and
  zero-count records are never revived. The item named exactly `Paint` tops
  off at **10**; other paints, modules, and boosters keep their **5**-item
  rule. Every `TimeLeft` field of type `FloatProperty` owned by an actor
  named `BP_Item_HumanSeed_*` is set to **99999**, regardless of inventory
  selection or the Refill Storage Tanks checkbox. Other timers are unchanged.
  The hidden Functional Rocket (`BP_Rocket_Functional_C_UAID_*`) is also
  filled every run, regardless of selection or Refill Storage Tanks:
  `Resources.Electricity` **60000**, `Resources.Methane` **3000**, and
  `Resources.Oxygen` **12000**. Its other resources and condition fields, and
  all other hidden UAID actors, are unchanged.
  The repair is written immediately; when **Settings > Combine duplicate carried
  stacks** is enabled (the default), duplicate carried stacks are then
  combined automatically and that change is written separately. **Remove Duplicate
  Stacks** finds carried items split across two or more redundant stacks of
  the same asset (and variation, if any) — e.g. three separate 999 stacks of
  `Iron` instead of one — keeps the single largest stack of each and deletes
  every other stack of it outright (not merged into the kept stack). Only
  carried records that can be safely removed structurally are offered.
  Weapon Attachments are never combined or deleted, whether installed on a
  weapon or loose in the backpack. Attachments installed on a weapon (entries
  of a weapon's nested `Attachments` map) keep their existing count during
  Repair & Top Off — only loose attachments are set to 10 — so the game never
  sees an installed attachment change and eject it. Structural removal also
  has a hard guard: it refuses to delete any attachment record, anything
  inside an `Attachments` map, or any weapon that still holds attachments.
  The standalone **Remove Duplicate Stacks** button protects Ammo, Memory,
  and Weapon items by asset name prefix (`Ammo…`, `Memory…`, `Weapon…`) as
  well as category, because saves store them under generic categories such as
  `Equipment`. The default-enabled **Combine** step in **Repair & Top Off**
  also removes duplicate carried Memory stacks, keeping the largest stack
  of each Memory item; this does not change the standalone button's behavior.
  grenades (any `Weapon…` asset containing `Grenade`, e.g. `Weapon GrenadePetrolBig`)
  are exempt, so duplicate grenade stacks are always reduced to the single
  largest stack, and the explicit Petrol/Oxygen/Electric grenade deletions still apply. If a specific item section can't be
  read to its own declared end (rare — a partially-parseable map), duplicate
  detection now treats every record in that section as unsafe to touch:
  reporting a record as purgeable and then only partially removing entries
  from a map is exactly how a save can end up with an incorrect declared
  entry count and orphaned leftover bytes later confusing the game's own
  loader, so this case is refused outright rather than risking that. Those
  records still show up in the editor's item list, and the status bar
  reports how many were *detected but can't be safely removed yet* instead
  of a misleading flat "none found" when that happens. Safe removals across
  nested `Items` and `Attachments` maps are collected against one immutable
  snapshot and applied atomically, so deleting a nested attachment cannot
  shift and invalidate the offsets later used for its enclosing item map.
  Standalone duplicate removal leaves every item in the `Ammo`, `Memory`, and `Weapon`
  categories untouched, except that duplicate grenade stacks are removed
  (keeping the largest) and the three exact player-backpack
  equipment records `Weapon GrenadePetrol`, `Weapon GrenadeOxygen`, and
  `Weapon GrenadeElectric` are always deleted when they belong to
  `BP_FirstPersonCharacter_New_C1` (the listed records currently have count
  1). **Repair & Top Off** sets every `Memory` stack's count to 10, then its
  enabled **Combine** step removes redundant carried Memory stacks after the
  initial save succeeds.
  Item stack limits follow these rules:

  | Item | Stack |
  | --- | --- |
  | Anything in the `Material` category, or starting with `Part` | 999 |
  | Anything named `Cable` (Repair & Top Off) | 100 |
  | Ammo (Repair & Top Off) | 2500 |
  | Food (Repair & Top Off; `Food` category or names `Food`, `Food ...`, `Food_...`) | 999 |
  | Big Petrol Grenades (Repair & Top Off) | 2500 |
  | Other grenades (Repair & Top Off) | 500 |
  | Weapon attachments (Repair & Top Off) | 10 |
  | Weapons that are not grenades or attachments (includes `Weapon Petrol Grinder`) | 1 |
  | Waterjets, headlights/flashlights, dismantle and repair tools, sea mines | 1 |
  | `Binoculars` or `ScanTool` | 1 |
  | Any `Cartridge*` item | 10 |
  | Anything starting with `Back`, `Prop`, `Memory`, `ItemLoot` | 1 |
  | The item named exactly `Paint` | 10 |
  | Anything starting with `Module` or other `Paint*` items, plus `Mooring Socket` | 5 |
  | Anything containing `Cylinder`, except `Part StorageCylinder` | 50 |
  | Anything containing `Glow` (`Equipment Glowstick`, `Equipment DepthGlow`) | 100 |
  | The item named exactly `Consumable ChargeInjector` (Repair & Top Off) | 500 |
  | Scraps, ore `Nugget`s, `Pigment*`, `Bio *`, and non-Material `Consumable*` items | 999 |
  | Everything else | 999 |

  Health is restored to the highest value the save records for that item
  (minimum 100).

  **Settings > Stack Targets...** opens a scrollable window
  that sets the count Repair & Top Off uses for every category and
  subcategory: Weapons (Guns & Tools; Grenades → Big Petrol, Other Grenades;
  loose Attachments), Ammo, Memory, Food, Cartridges, Resources (Materials, Parts, Cables,
  Scraps, Ore Nuggets, Pigments, Bio Matter), Consumables, Consumable ChargeInjector,
  Modules (Modules & Sockets, Enhancers, Cylinders), Paint
  (exact `Paint`, Paint Designs), Equipment (Back Batteries & Tanks, Tools &
  Lights, Glow Sticks & Depth Glow), Props & Loot, Everything
  Else, and Not Carried.
  **Food** defaults to 999 and is saved with the other category targets;
  per-item overrides still take precedence for carried food. The v6.0 item
  families seen in live saves have their own carried-item targets, all 999 by
  default: **Scraps** (names ending `Scrap`/`Scraps`), **Ore Nuggets**
  (`... Nugget`), **Pigments** (`Pigment ...`), **Bio Matter** (`Bio ...`),
  and **Consumables** (`Consumable ...` outside the `Material` category, such
  as `Consumable BatteryLarge`). **Consumable ChargeInjector** has its own
  Stack Targets entry, defaulting to **500**, independent of the general
  Consumables target. Per-item overrides still take precedence. These items no longer use the Everything
  Else target. Rows
  marked `*` (grenades, attachments, Ammo, Memory, Cartridges) apply wherever
  the item is; every other non-carried record uses the **Not Carried** target. Values
  must be whole numbers from 1 to 2147483647; **Reset to Defaults** restores
  the table above. Targets are saved immediately to `editor_settings.json`
  (`repair_stack_targets`). Memory items are now recognised by their
  `Memory…` asset prefix too, because saves file them under the generic
  `Item` category — previously they fell through to a stack of 1. Cables are
  likewise matched on the name (`Cable …`, or `Cable` as a word) *before* the
  `Material` category is consulted, so `Cable Electricity` — which the save
  files under `Material` — tops off at the Cables target (100) instead of
  being swept up with materials at 2500. **Cartridges** are matched by their
  `Cartridge*` name before generic save categories such as `Ammo`, and target
  10 by default; this prevents `Cartridge Javelin` from being topped off to
  the Ammo target. **Cylinders** are matched the same
  way: any item with `Cylinder` in its name — engine cylinders, their
  `Booster`/`SuperBooster` variants, and `ItemLoot Trash Metal Cylinder …` —
  lands in the **Cylinders** category (50 by default), whatever its save
  category. The one exception is `Part StorageCylinder`, which stays with
  Parts/Materials so it keeps topping off at 2500. This replaced the old
  `modules.boosters` category key, so an existing `editor_settings.json`
  falls back to the new default until you set a Cylinders value. **Glow
  items** follow the same name-first pattern: anything with `Glow` in its
  name (`Equipment Glowstick`, `Equipment DepthGlow`) uses the **Glow Sticks
  & Depth Glow** target (100 by default) rather than being swept into
  Everything Else. Manual
  stack editing and duplicate removal are unaffected.

  **Per-item stack sizes** sit at the bottom of the same window and win over
  the category rules for that exact item name (case-insensitive). Use
  **Capture From Save** to store every carried item's current stack size, so
  Repair & Top Off rebuilds today's inventory exactly every time — including
  one-off amounts that no category can express (`Bio Flesh` 5000,
  `Weapon Grenade Decoy` 500, `Cable Electricity` 100, `Prop Boom Box` 1).
  Blank a value to drop that override, or use **Clear Per-Item** to remove
  them all and fall back to the category table. Overrides are stored in
  `editor_settings.json` (`repair_asset_targets`) and still respect the
  scoping rules: an override never revives or inflates an ordinary record the
  player is not carrying, and attachments installed inside a weapon are never
  touched.

  **Delete Items...** opens a configurable deletion list for Repair & Top Off.
  The list starts empty: Trash, Scrap, and all Bio items are now kept and
  topped off normally unless you explicitly add their exact names.
  Filter the inventory drop-down using the usual wildcard/exclusion syntax,
  then click a name in it to add that item to **Marked for deletion**.
  Select marked names and use **Remove from List** (or Delete) to unmark them.
  **Save** persists the list in `editor_settings.json` (`repair_delete_items`);
  **Cancel** discards list changes. Editing this list does not delete anything
  immediately. Names remain configured even if absent from the current save.
  On Repair & Top Off, all carried copies matching those names
  (case-insensitive, exact name) are structurally deleted after the initial
  save succeeds and before the optional Combine step, independent of inventory
  selection. No deletion occurs if the initial save fails. World/container/
  catalog copies and weapon attachments remain protected.

  Repair & Top Off, duplicate removal, blueprint scanning, and the survival
  locks reload the file first if the game has rewritten it since the editor
  loaded it. Without that reload the editor compared the configured target
  against stale counts — an item already at its target when the save was
  loaded produced no edit at all, so a stack the game had since spent down
  (2500 → 2352) stayed where it was, and the write then failed its own
  on-disk consistency check behind an error dialog. Queued human profession
  and stat saves refuse stale files instead, keeping the pending edits until
  you choose to reload.
- **All Scalars**: inspect, filter, sort, and edit all safe, fixed-width Unreal
  `IntProperty`, `FloatProperty`, `DoubleProperty`, and `BoolProperty` values.
- **Backups**: after the initial save loads successfully at startup, the
  editor automatically creates a timestamped `.bak` restore point (no prompt),
  using the current **Settings > Backups > Keep backups** count. Nothing is backed up if no save
  loads; reloads and saves opened later do not create one automatically.
  Ordinary saves and immediate structural removals do not create backups.
  Saves that include human profession or stat changes, and experimental blueprint
  scanning, create a backup before writing. Use
  **Create Backup Now** in the Backups tab whenever you want another restore
  point. Backups can be listed, pruned, and restored from the GUI.
- **On launch** the editor opens maximized (full screen). By default, it loads the most
  recently modified discovered save (regardless of which save was open last
  session), and backs it up. That is often the game's own
  `..._AutoSave_0.sav`, which is much larger than a manual slot (about 146 MB
  once decompressed) and takes about half a minute to read the first time; the
  window stays responsive and the footer names each stage (see
  [Keeping the window responsive](#keeping-the-window-responsive)). The
  Inventory tab starts on the **Carried (all)**
  location. Settings **Show zero-count catalog**, **Include environment/world in Purge**, and
  **Combine duplicate carried stacks** default to checked and remember your choices.
  Use the save selector or **Browse...** to switch saves.
- Ordinary scalar edits and queued human professions/stats stay pending until
  **Save Changes** is clicked; the maintenance actions described above save
  immediately.
  The editor refuses to
  write while the game process is running and detects saves changed on disk.
  The **Settings > Warn before saving** checkbox controls whether
  a confirmation dialog appears each time; unchecking it (remembered between
  runs) makes Save Changes write immediately. Saves that include human
  profession or stat changes always ask first.

## List controls

- Inventory rows are color-coded by item type: ammo is light blue, memories
  violet, weapons soft red, food green, resources/materials sand, parts teal,
  cables cyan, consumables yellow-green, modules/cylinders indigo, equipment
  blue-gray, paint pink, cartridges lavender, and props beige. Unrecognized
  items remain white. Colors use the existing item classifier (including
  names when the saved category is generic); asset/category text stays visible
  and normal selection highlighting takes precedence. Colors survive filters,
  sorting, and queued-value updates without changing the save.
- The save selector and Inventory controls use multiple rows so every button
  remains visible at lower window widths and display resolutions.
- Text filters only apply when you press **Go** or `Enter`; typing no longer
  re-filters the list on every keystroke. **Clear** empties the filter and
  restores the full list. The Location dropdown still applies immediately.
- Sorting reorders the list in one bulk operation, including when **Clear**
  restores a sorted **All Scalars** list. This avoids the long freeze caused
  by moving hundreds of thousands of rows individually; no results are capped
  or omitted.
- Inventory is sorted by **Category**, then **Name** by default; clicking a
  column header re-sorts and that choice is remembered across reloads.
- Editing a value or saving updates the affected cells **in place**, so rows
  never jump, re-sort, or lose your scroll position and selection.
- Type the first letter(s) of an item to jump to it; repeating the same letter
  cycles through matches.
- `Home`, `End`, `Shift+Home`, `Shift+End`, and `Ctrl+A` work in every list.
- `F5` reloads the save from disk and warns first when unsaved edits exist.
- Right-click a row for **Copy Cell**, **Copy Column (selected rows)**,
  **Copy Row(s)**, and **Copy Row(s) with Headers**. `Ctrl+C` copies the
  selected rows. All copy commands honor multi-selection and produce
  tab-separated text for pasting into a spreadsheet.

## Saved-position map

Open **Map** to build a top-down coordinate map of the loaded save.
The map is read on a background worker only when needed; opening other tabs
does not add a map-parsing step to save loading. It plots actual saved actor
positions, **not terrain, coastlines, routes, or every object in the game**.
No game installation or network connection is required.

**Player**, **Humans in pods**, and **Named locations** are enabled initially.
Layer checkboxes in **Settings > Map layers** also
show other human pods, functional rockets, checkpoints, containers/crates, and
other saved actors. The layer list scrolls and remembers enabled layers between launches.
A pod without a grown human may still contain a seed;
that layer does not assert that every pod is completely empty. Counts beside
the layers describe the selected world's available objects.

### Named locations

**Named locations** shows places such as Lazarus Complex, Platform Omega,
Exodus Station, refuel outposts, seed reserves, and Gyrocopyter platforms as
labelled squares. The save stores only these places' identities and flags in
the world's `Locators` list — **not their coordinates** — so the positions come
from the bundled `named_locations.json`, extracted read-only from the game's
`VoyageWorld2` level (116 places, game v6). The details panel states that the
position is game data and whether the place is listed in this save's
map-marker list. The **Checkpoints** layer is the separate set of respawn/save
modules, not named places.

After a game update, regenerate the file (the game is never modified):

    python extract_named_locations.py "D:\Games\The.Last.Caretaker.v6.0"

It needs the `ooz` package for the game's Oodle-compressed files. If the JSON is
missing or invalid, the map still loads without this layer and the status line
says why.

**Custom in-game waypoints are not saved.** The save has one
`3_MINIMAP_WAYPOINT` entry (flags only, no position). In a controlled test
(waypoint placed near Platform Omega, then a manual save), that entry and the
whole `Locators` list stayed byte-identical. No new coordinates or waypoint
data appeared anywhere in the save or config files; only the player position,
timestamps, and ordinary actor state changed. The game evidently keeps
waypoints only in memory, so a save editor cannot add a waypoint that appears
in game. Use the Map's **Find** and object details for coordinates instead.

Use **Find > Go** or `Enter` to search names, actors, Social IDs, or saved
professions with the normal wildcard and exclusion syntax; **Clear** removes
the search. Hidden layers stay hidden during a search. Choose a world with
the **World** selector when the save contains several.

Drag the map to pan, use the mouse wheel or **Zoom + / -**, and click
**Fit visible** to frame the matching objects (when many objects match, a few
far outliers such as North Bastion, about 100 km south, are left outside the
frame; search for one to frame it). **Center on player** uses that
world's unique saved player position. With the canvas focused, arrow keys pan
and `+` / `-` zoom. **North is up**: in this world north is **−Y** (North
Bastion and North Lantern sit at negative Y, South Echo at positive Y), so +Y
runs down the screen and +X (east) runs right, matching an unmirrored top view.
Grid labels show the raw saved X/Y values.

Click a marker to see its identity, saved world coordinates in meters, height,
and straight-line 3D distance from the saved player position. Human markers
include the Social ID and saved profession when available. Selecting a marker
opens the **Object details** panel; switch back to **Layers** to change layers.
Nearby objects
share a numbered marker (which may contain several layers): select an object
in the inspector, or double-click
the marker to zoom in. **Copy** copies the inspector text.

### Recover a loose human pod or egg

Select one saved human pod or loose human-seed item, then use **Move selected
to player (1 m north)**. The destination is calculated from the save's last
saved player position, not the live in-game position. The editor asks for
confirmation, makes a backup using the configured retention count, updates the
actor and its verified component transform, saves, and reloads the map.

For safety, buildings and other actor types are not movable. Attached actors,
unsupported component layouts, stale map data, and destinations in a different
saved world cell are rejected before the backup or save is written. The map has
no terrain or collision data, so it cannot promise that the destination is
clear or on a ship deck or water surface. Close the game and save or discard
queued editor changes before moving an object.

The map reports skipped/unsupported positions and unreadable human-pod states
instead of inventing positions or occupancy. **Refresh map** rebuilds from the
currently loaded document; **Reload** (`F5`) first reads changes made on disk.
Changing map filters, layers, selection, zoom, or pan never queues edits or
writes the save; only the explicit **Move selected to player** action does so.
Reloading a save refreshes the map data; layers and search stay as chosen during
the current app run.

## Humans and committee professions

Open **Humans > Roster** to view the humans already stored in the save's
`HumanGrow` roster. **In space** means the saved result is `Delivered`;
**In pod** means a successfully grown human has a safely located occupied pod.
Other result types keep their saved labels instead of being treated as delivered.
Unfinished growth without a roster record is not included.

Names are generated by the game and are not stored as readable roster names.
Use the **Social ID** shown in the game's human details to identify a person.
The roster searches Social IDs, professions, other traits, committee job
matches, and worlds with the same **Go / Enter / Clear**, wildcard, and
exclusion rules as the other tabs. The **Location** dropdown beside the filter
applies immediately.
Rows can be sorted and copied; selection survives a save/reload by Social ID
and world rather than by shifting byte offsets.
The roster also shows **Pod X / Y / Z (m)** for humans in occupied pods and
their straight-line **3D distance (m)** from the saved player position. These
are world-map coordinates in meters; distance includes height. A dash means
the pod coordinates or player position are unavailable in the save.

To change jobs, select one or more humans (`Ctrl`/`Shift`-click selects
several) and choose a **Profession**. The choice is **queued**, not written:
the row shows `New (unsaved; was Old)`, the footer counts the queued
profession changes, and **Committee roles** previews the new coverage. Select
other humans and queue different professions as often as you like; queued
changes survive selection, filtering, and sorting. Choosing a human's saved
profession again removes that human's queued profession change. **Discard
Human Changes** drops all queued professions and stats after asking, without
discarding ordinary scalar edits.

To change stats, select **one human**, then select an attribute below the
roster and click **Edit Stat...** or press `Enter`. Double-clicking a raw
value opens the same dialog with that field focused. Edit **Roster (raw)**,
**Pod current (raw)**, and **Pod simulated (raw)** independently; only fields
actually stored for that human are editable. Values shown as `-` are absent
and cannot be added. Each field shows its saved value alongside the pending
value. Click **OK** to queue the edits, or **Cancel** to leave them unchanged.
Only finite numbers are accepted; decimals, negative values and scientific
notation are supported, but `NaN` and infinity are rejected.

Queued stats show `*` in the attribute table and `(unsaved stats)` in the
roster. You can edit several stats and humans before saving; the queue survives
selection, filtering and sorting. Entering a field's original saved value
removes just that field's pending edit. These are **raw saved values**, not
converted in-game values: height, strength and life-expectancy fields may hold
growth points rather than physical units. No age formula or stat limits are
guessed, and timestamps remain read-only.

**Save Changes** (on the Humans tab or in the top toolbar) writes
every queued profession and stat, together with any pending scalar edits, in
**one** verified write. Each queued profession change replaces **all** profession traits for that
human with the chosen single profession. Choose **(No profession)** to clear
profession traits only. Unknown/future professions remain visible and
untouched unless that human's professions are explicitly replaced. Mutations
and other non-profession traits are preserved.

Close the game first. A save that includes human profession or stat changes always asks for
confirmation, independently of **Warn before saving**, and lists the changes.
The editor creates a backup using **Settings > Backups > Keep backups**, writes, and
reloads the save. If the write fails, the save file is left unchanged and the
queue is kept so you can retry. Queued changes that already match the save do not
rewrite the file or create a backup. Stale files, malformed human data, and
ambiguous or missing pod links prevent the save rather than risking an
incomplete rewrite. Reloading, opening another save, and closing the editor
warn before discarding queued human changes. Repair & Top Off and the other
structural actions (purges, duplicate removal, configured deletion, blueprint
scanning, survival locks) ask you to save or discard queued human changes first.

For an occupied pod, the roster's `Traits` and that pod's `TraitsCurrent` /
`TraitsSimulated` are changed together. All enclosing sizes and byte-array
counts are updated and the result is re-read before the normal verified,
atomic save write. Profession changes do **not** change growth stats unless
you also queue stat edits. Stat edits replace only the selected existing
double-precision values, without resizing their maps or copying values to the
other roster/pod columns. Stats are patched before structural profession
changes, so changing both in one save keeps offsets correct.
Timers, timestamps, Social IDs, launch/delivery status, and committee state
remain unchanged. The game may recalculate a pod human's stats and profession
during growth; in-game recalculation has not been disabled. Restore the backup
if the game does not retain the desired changes.
Creating/deleting humans, editing generated names, and forcing launches are
not supported.

**Humans > Committee roles** lists the four required professions for each
v6 committee, how many roles have matching delivered humans, and their Social
IDs. Humans still in pods do not contribute to this view; several people
with the same profession still fill only one required role. The view is
**profession coverage, not an editable membership list or proof of readiness**.
The save does not contain an explicit per-person committee assignment in
these records. All v6 roles require age 18+, and the game applies age and
other eligibility rules independently. Unknown saved committees are listed
without inventing requirements.

## Where "Player Inventory" really lives

The player pawn does not have one inventory — it carries **eight** separate
`Items` maps, and older builds of this editor lumped them all together under a
single "Player Inventory" label. That is why records such as
`LightLancer HeadLight` or `Nebula HeadLight` appeared to be in your inventory
but never showed up in game: they are catalog/unlock entries, not carried items.

Real inventory components carry a `Name` text property pointing into the
`/Game/LocalizationStringTables/ST_Inventory.ST_Inventory` string table. The
editor reads that key and classifies each map accordingly:

| Location | Source | In game? |
| --- | --- | --- |
| `Player Backpack` | component named `Backpack` | yes |
| `Player Holster` | component named `Holster` | yes |
| `Player Equipped` | component named `Equipment` | yes |
| `Player Crate` | a player-created `PlayerBox` storage actor | yes |
| `Player Module` | unnamed map with a `ModuleState` sibling | module mirror |
| `Player Catalog` | unnamed maps | no — recipes/unlocks |

`Player Backpack`, `Player Holster`, `Player Equipped`, and player-created
`Player Crate` storage all count as **carried**. The Location filter offers
each one individually plus a `Carried (all)` shortcut. This means **Repair &
Top Off** and **Remove Duplicate Stacks** include items in player crates by
default; catalog and module records are left alone unless explicitly selected.
Records stored *inside* another item (the paints loaded in a paint item, a
cartridge holder's slots, and similar) are that item's contents, not separate
stacks, so **Remove Duplicate Stacks** never counts or deletes them.
**Purge Empty Slots** can also reach environment, container, and world records
when **Include environment/world** is enabled; see the next section.

### Backpack mirroring

The backpack map is stored **twice**. The `Backpack` component and the module
component hold byte-identical payloads at a fixed offset delta, with all 300
records pairing one to one. Editing only one copy would leave the save
internally inconsistent, so the editor detects the mirror on load and writes
every count, health, and mass change — and every permanent removal — to both
copies. Mirrored twins are hidden from the results summary, so a report of
"1 removed" means one item you can see, not one raw entry.

### Manual saves and AutoSaves

The game writes two differently organized files. A manual slot
(`<SteamId>_0.sav`) files the player's actor under
`PersistentActors["pawns"]`. The game's own `<SteamId>_AutoSave_0.sav` has no
`pawns` entry at all: it files the player under the world-partition cell the
player is standing in (a key such as `119:43`), and its newest write is usually
the newest file in the save folder, so it is what the editor opens at launch.

The editor therefore identifies the player by the persistent actor that wrote
each `Items` map (its `ActorName`, a `FirstPersonCharacter`/`PlayerCharacter`
actor) as well as by the `pawns` key. Backpack, Holster, Equipped, Catalog and
Module records are classified, mirrored and edited the same way in both files.
Earlier versions only recognized `pawns`, so an AutoSave listed every carried
item as `Player Backpack` and the Holster and Equipped items seemed to be
missing.

If the editor ever finds carried items it cannot assign to the player's
inventory components, it tells you instead of guessing: the footer ends with
`WARNING: ...` and an **Inventory structure** dialog appears once the load
finishes. In that state Holster, Equipped, Catalog and Module records may be
listed as Backpack and the structural tools (Purge Empty Slots, Remove
Duplicate Stacks, mirrored writes) may skip them, so do not rely on those tools
until the warning is understood.

## Deleting items
Setting a record's count to zero does not really delete it. The game keeps the
slot and shows it grayed out with a count of `0`, and those slots cannot be
dropped, moved, or otherwise cleared in game. Removing them for real means
deleting the entry from the save's `Items` map, which changes the payload
length — so every enclosing container header has to shrink with it.

The editor does this through `voyage_gvas.py`, a structural parser for Voyage's
property format:

- **Purge Empty Slots** deletes every zero-count carried record in one pass.
  Weapon attachments (including loose ones the game leaves at zero) and weapons
  that still hold attachments are always kept, and they are left out of the
  "Remove N record(s)?" figure, so the number you confirm is the number removed.
  Check **Include environment/world** first to also sweep zero-count records
  everywhere in the save (containers, crates, and dropped world items) —
  this is the fastest way to clear out large numbers of lingering ground
  pickups at once.

Both rewrite the save immediately rather than queueing an edit, because the
removal shifts every offset after the cut. The result is re-validated and
re-parsed before it is written, and the compressed container is decompressed
again and compared byte for byte. Use **Create Backup Now** first if an
additional restore point is needed.

**Tree-walk safety.** `find_item_maps()`'s sibling-scanning walk is now bound
to each enclosing container's own declared end, so it can never read past a
struct/map-entry/byte-blob boundary into unrelated bytes. An earlier version
could, for some non-pawn actors, drift past a component's real end and
attach the wrong ancestor chain to an `Items` map it later found — which
would have made a deletion shrink the wrong container's `Size` header. This
was caught by cross-checking every discovered map's chain against an
independent top-down walk (`find_path`) across the full save (1,134 item
maps, 0 mismatches after the fix, versus 22 duplicate/misattributed maps
before it) and is now covered by a regression test.

Pending edits must be saved or discarded first, and the game must not be
running.

In a manual slot the player's inventory lives at
`GameProgress → PersistentActors["pawns"] → actors → ComponentData → ByteData →
Items`; in an AutoSave the same chain runs through the world-cell key instead
of `pawns` (see [Manual saves and AutoSaves](#manual-saves-and-autosaves)).
Deleting one entry updates, in order: the `Items` map's element count,
the `Items` `Size`, the `ByteData` blob's element count and `Size`, and the
`Size` of `ComponentData`, `actors`, `PersistentActors`, and `GameProgress`,
plus the payload's outer length prefix.

### Property format notes

Voyage's property tag is not the classic Unreal layout. It is:

| Field | Type | Notes |
| --- | --- | --- |
| Name | `FString` | `None` terminates a property list |
| Type | `FString` + `int32` child count + that many child nodes | a recursive type tree |
| Size | `int32` | length of the payload |
| Flags | `uint8` | UE 5.4 `EPropertyTagFlags` |
| Payload | `Size` bytes | |

`Flags` is a bit field, not a boolean: `0x01` adds an `int32` array index,
`0x02` adds a 16-byte GUID, `0x04` adds tag extensions, `0x08` marks the value
as serialized-zero, and `0x10` carries a `BoolProperty`'s value. Treating it as
a plain "has GUID" byte mis-parses `OptionalProperty` records.

A `MapProperty` payload is `int32 NumKeysToRemove`, `int32 Num`, then `Num`
untagged key/value pairs. `ByteData` is an `ArrayProperty<ByteProperty>` whose
payload is an `int32` byte count, one version byte, and then an ordinary
property list.

## Mass and OverrideMass

Neither field is item weight, which is why editing them appears to do nothing.

**`Mass`** (1,204 records) is serialized inside the
`PersistentWorldComponentTransform` struct, next to `WorldComponentTransform`,
`LinearVelocity`, `AngularVelocity`, `CollisionEnabled`
(`ECollisionEnabled::QueryAndPhysics`), and `CollisionObjectType`
(`ECC_PhysicsBody`). It is the Unreal rigid-body mass in kilograms captured as
part of a physics snapshot so a simulating object restores with the same
motion state. Observed values are real physical masses (2.59, 6.33, 8.92,
9.46, 75.0 kg), with a very common `0.001` — Unreal's minimum-mass floor for
bodies whose computed mass is effectively zero.

**`OverrideMass`** (809 records) is serialized in the dynamic mesh actor's own
record next to `bSimulatePhysics`, `bAwake`, `BuoyancyMultiplier`,
`bInventorySupported`, `SavedBoneTransforms`, and `bCanBeDamaged`. The shipping
binary exposes a setter taking `InOverrideMass` alongside
`InBuoyancyMultiplier`, mirroring Unreal's `FBodyInstance::bOverrideMass` /
`MassInKgOverride` pair. `0` means "no override — derive mass from the mesh",
and saves that do populate it write `0.0001`, the engine's effectively-unset
sentinel. It has never been observed holding a meaningful custom mass.

**Why edits do not stick.** The values are a volatile runtime snapshot. On load
the game rebuilds the physics body and recalculates mass (`CalculateMass`,
`SetPrimitiveInertiaTensor` in the binary); on the next save it rewrites the
fields. This was confirmed directly: two consecutive game saves three minutes
apart decompress to the identical 9,535,014-byte payload, yet the game changed
1,210 of 1,235 differing byte runs — every one of the 1,204 `Mass` floats was
rewritten from a real value to `0.0`. The only other differences were ordinary
item-count changes from playing.

**What mass is actually used for.** The binary pairs `MaxMassGrab` with
`MassGrabStrength`, so rigid-body mass governs what the caretaker can pick up,
carry, and throw, and it feeds buoyancy through `BuoyancyMultiplier`,
`BuoyancyCoefficient`, and `MaxBuoyantForce`. It does not feed inventory
capacity.

**Where item weight really lives.** The save contains no `Weight`,
`ItemWeight`, `MaxStackSize`, or `Density` property at all. The binary's
`MaxItemWeight`, `MaxConfigurableItemWeight`, `OnInventoryWeightChanged`, and
`MaxStackSize` symbols belong to inventory widgets that read the `DA_Item_*`
data assets packed in `pakchunk0-Windows.utoc`. Those are cooked read-only
content, so per-item weight and stack limits cannot be changed from a save
file.

The Inventory tab does not expose `Mass`/`OverrideMass` for editing since
changes to them do not persist across a save/load.

## Carried weight and speed

Stacking everything to 999 can push total carried weight far past the
character's base carry limit, causing the heavy slow-walk penalty. The raw
per-item weight and the base carry limit are still not stored in the save
(see below), but carry capacity is not fixed — it is raised by spending
**Enhancer points**, and those points ARE stored in the save. See
"Enhancer points (the Scaler)" below; setting `Character HaulMastery` high
is what actually fixes the slow-walk problem, not a hidden weight/speed
field.

- Per-item weight lives only in the cooked, read-only `DA_Item_*` data
  assets — the same place stack limits live — so it is never in the save.
- Total carried weight (`GetTotalCarriedWeight`, `CalculateInventoryTotalWeight`)
  is computed live from those data assets on every load; it is not a stored
  number either.
- There is no serialized `MaxWalkSpeed` or speed-multiplier override, so
  speed can only be fixed indirectly, by raising carry capacity (or reducing
  carried weight) enough that the game's own encumbrance penalty stops
  applying.

Two Inventory tab actions also help manage carried weight, though enhancer
points (above) are the real fix:

- **Repair & Top Off** refills carried items to their normal stack rule
  (materials/parts/cables/ammo to 999), and never tops off anything the
  player is not carrying — those records are instead compacted to a stack
  of 1. The repair is saved immediately. When **Settings > Combine duplicate carried stacks** is
  enabled (the default), duplicate carried stacks are then combined and that
  structural change is saved separately.
- **Set Count...** lets you set an exact count on any selected record(s),
  which is useful for manually lowering a carried stack to reduce weight.

Lowering carried stacks is the only lever the save format exposes for
reducing carried weight, short of raising Enhancer points.

## Enhancer points (the Scaler)

The `PecoLockStates` actor stores one `Points` `IntProperty` per **Enhancer**
(shown in-game under the Scaler/perk screen): `Battery`, `Equipment
Handling`, `Character Vitality`, `Character ShockAbsorber`, `Character
Scrapper`, `Character Pressure`, `Character Power Management`, `Character
Improved Hydraulics`, `Character HaulMastery`, `Character FieldMedic`, and
`Character Engineering`. These are the spendable enhancer/perk points, not a
raw stat — e.g. raising `Character HaulMastery` is what actually increases
carry capacity (confirmed: setting it to 9999 let a 200 kg base carry limit
hold roughly 50,000 kg in game).

The Progress tab lists each one as **Enhancer: <name>**, editable the same
way as any other stat row — select several enhancer rows (Shift/Ctrl-click,
or `Ctrl+A` for every row) and edit them in one prompt to set the same value
across all of them at once. There is no known hard cap in the save format
itself, but some enhancers gate a mechanic rather than a pure stat — e.g. a
`Character Scrapper` value that is too high has been observed to make
scrapping items stop working — so raise these gradually and verify the
related mechanic still works in game.

## Robot charge

The game executable defines `EVoyageSurvivalAttributeType::Charge` alongside
Health, Stamina, and Level. The current save's player `SurvivalAttributes` map
contains only Health and Level, so Charge is not currently persisted there.
The Progress tab detects named player stat blocks dynamically: if Charge is
serialized after an equipment or progression change, its current, minimum,
maximum, and default values will appear automatically.

The `Level` survival attribute is the caretaker's energy/power meter, so the
Progress tab labels its four rows **Player Charge - Current / Minimum /
Maximum / Default**. The save still stores them under `Level`; only the
displayed label changes. The separate integer **Caretaker Level** progression
field is unaffected.

## Infinite health and energy (trainer locks)

**Game v6 and later:** the player's `SurvivalAttributes` now store only the
current `Value` (no Min/Max/Default), so the lock hack below cannot work and
its checkboxes are disabled. Raise capacity with enhancer points instead:
**Progress → Boost Health/Energy Enhancers...** stages one value (default
`999999`) on `Character Vitality` (health), `Character Battery` and
`Character Power Management` (energy) as pending edits; review them and
**Save**. Close the game first, then verify the new maximums in game — if a
very large value misbehaves, lower it as with other enhancers.

Older saves:
Trainers make health/energy permanent by pinning the player's
`SurvivalAttributes` entries (`EVoyageSurvivalAttributeType` →
`VoyageSurvivalValue`) so the minimum can never drop below the current value.
Because the save stores those floats, the effect survives quitting the game
and closing the trainer. The player's map is the only one holding both
`Health` and `Level`; `Level` here is the energy/power meter (not the
integer caretaker level).

| Attribute | Normal (untouched save) | Locked (trainer) |
| --- | --- | --- |
| Health | Min 0, Max 200, Default 100, Value 200 | Min = Max = Default = Value = 200 |
| Energy (`Level`) | Min 0, Max 100, Default 1, Value 100 | Min 200 > Max 100, Default = Value = 200 |

An attribute counts as locked when its minimum is at or above both its
maximum and current value. **Settings > Player locks** shows **Infinite Health** and
**Infinite Energy** checkboxes reflecting the loaded save; ticking or
unticking one asks for confirmation, backs up, writes those four floats in
place (same width, no structural change), and reloads. Close the game and
save or discard pending edits first. Unlocking restores the normal values
from an untouched save; if the game restores the lock from elsewhere, restore
the backup.

## Save format

Voyage does not store a plain GVAS file. It wraps a length-prefixed Unreal GVAS
payload in concatenated 128 KiB Unreal compression blocks:

1. 8-byte Unreal package/compression magic.
2. 64-bit chunk size.
3. Compression method `2` (Oodle/Kraken, newer saves) or `3` (zlib).
4. 64-bit compressed and uncompressed sizes.
5. Repeated size pair and the compressed stream.

Both methods are accepted when loading, checking for external changes, and
restoring backups. Writes use the existing Unreal method `3` (zlib) container,
including when the input was Oodle-compressed; they do not relabel Oodle bytes
as zlib. Backups retain the original file's compression and bytes.

Newer saves may also omit survival `DebugName`, minimum, maximum, and default
fields. The Progress tab uses the saved survival-map keys to display available
current values (Health, Level/energy, Temperature, etc.). Missing bounds are
not invented: Infinite Health / Energy controls are disabled for attributes
without the serialized limits required for those locks.

The property list starts right after the GVAS header, at an offset that depends
on the header's variable-length fields. The editor tries each candidate offset
and accepts the first one whose tag and type names are plain printable ASCII and
whose top-level list parses cleanly to its `None` terminator. A shifted read can
decode a few stray bytes as a plausible-looking tag (one AutoSave produced such
a match one byte too early, which made the whole item structure unreadable), so
a merely plausible tag is only a last resort. If no candidate works at all, the
save still opens for scalar editing, its item structure is reported as
unreadable, and the warning described under
[Manual saves and AutoSaves](#manual-saves-and-autosaves) appears when carried
items are affected.

The editor validates every block, edits only same-width scalar bytes in the
decompressed payload, recompresses each block, decompresses the result again
for byte-level verification, then atomically replaces the original after
creating a backup. Container and GVAS envelope lengths are validated on every
load and write.

### Performance

Finding every `Items`/`Attachments` map means walking the whole decompressed
save (about 146 MB for the game's AutoSave), which takes roughly 30–40 seconds
in Python. That walk used to run twice per
load and again for attachments and duplicate detection, and every save
reloaded from scratch, so a single edit could take several minutes. Now:

- One walk collects both `Items` and `Attachments` maps (identical results to
  separate walks), and the result is cached by save content: its length plus a
  BLAKE2b digest of the decompressed bytes, so a reloaded copy of the same save
  is a cache hit. Only the walks of the two most recently used save contents
  are kept (the current save and the one before it). Earlier versions pinned a
  full copy of the save for every cached version, which grew to about a
  gigabyte after a single Repair & Top Off.
- A same-width value write (counts, health, stats — anything but a
  BoolProperty, whose value lives in the tag flags byte) cannot move any
  offset, so the cached structure carries straight over to the reloaded save.

The first load after launch still takes one walk. Measured on the game's
~146 MB AutoSave (213,485 values, 16,924 items): about 36 seconds for a cold
load, and 7–8 seconds to reload the same content, for example after **Save
Changes**. Structural changes (removing duplicates or empty slots, marking
blueprints scanned) change offsets and need one fresh walk, so they take the
cold time again. A manual save slot is only about 10 MB decompressed and is
far quicker.

**Large saves can still take considerable time when removing or combining
items.** After the automatic save, Repair & Top Off deletes carried items
explicitly configured in **Delete Items...**, and — when Combine is checked —
also combines duplicate carried stacks. Each structural change still re-walks
the save to verify the header math, a safety check retained after a real
save-corruption bug.

The structural walker now reuses already-decoded map and struct-array
properties instead of parsing them twice. Bulk deletion computes removed-byte
totals with indexed ranges and copies surviving bytes in one pass, rather
than shifting the rest of the save once per deleted record. Operations that
remove nothing no longer reload the unchanged save. Compression verification,
stale-save checks, attachment protection, and mirrored-inventory handling
remain in place.

An alternating in-memory benchmark with 25,000 items and 2,500 nested
attachment maps measured a structural walk at 0.745s before versus 0.576s
after, and deletion computation at 3.290s versus 0.387s (about 1.3x and 8.5x
faster respectively). The deletion timing excludes the mandatory verification
walk, compression, disk write, and GUI reload; these synthetic timings are
not a promise of the same overall speedup on a live save.

While loading, saving, scanning, or rewriting a save, the footer shows the
current processing phase with an indeterminate progress indicator and the window
uses a wait cursor; both are cleared on success or failure. How the window stays
alive during that time is described next.

### Keeping the window responsive

Earlier versions did all of that work on the same thread that draws the window.
A 36-second load of the AutoSave (or a Repair & Top Off that saves, deletes and
combines in a row) left Windows without window messages for the whole time, so
the title bar turned "Not Responding", clicks piled up, and the only way out was
to end the process. Now:

- **Heavy work runs on a worker thread.** Decompressing, scanning, rewriting,
  recompressing and writing a save, checking whether it changed on disk,
  checking whether the game is running, the empty-slot and duplicate-stack
  scans, blueprint marking, the survival-lock toggles, backup, and restore all
  run off the UI thread. The UI thread services the window about every 30 ms
  while it waits, so the window keeps repainting and Windows does not mark it
  unresponsive.
- **The footer says what is happening.** It shows the operation, the current
  stage, and the elapsed time once it passes a second, for example
  `Loading <SteamId>_AutoSave_0.sav - Scanning values (12 s)`. A load
  goes through *Decompressing the save*, *Scanning values*, *Reading the item
  structure*, and *Finishing*.
- **Input is ignored until the operation ends.** Mouse clicks and keystrokes
  (Tab and F5 included) are blocked while the worker runs, and a command that
  was already queued, such as a second click on **Save Changes**, is dropped
  instead of running afterwards. Nothing you do during the wait can start a
  second operation on top of the first.
- **Closing the window during an operation asks first.** The **Operation in
  progress** dialog defaults to **No**. **Yes** saves the editor settings and
  exits immediately. A save is written to a temporary file and swapped in only
  when it is complete, so quitting never leaves a half-written save. A backup
  that was still being copied may be incomplete, and Repair & Top Off is a chain
  of separate steps (save, remove the configured items, combine duplicates), so
  quitting part-way keeps the steps that already finished and skips the rest.
- **Memory stays bounded.** Only the two most recent saves' item structures are
  cached (see [Performance](#performance)), so repeated loads, saves and a
  Repair & Top Off settle at roughly 0.9 GB for the AutoSave instead of
  creeping past 1.3 GB. While a save is being rewritten and reloaded, memory
  still briefly peaks near 1.5 GB.

A few short steps still run on the UI thread: refilling the inventory lists
after a load (about 1.3 seconds for the AutoSave's ~17,000 items) and
re-evaluating the values after an edit (about 0.5 seconds). Measured on the
AutoSave, the longest stretch in which the window did not service events during
a load, a save or a Repair & Top Off was about 1.7 seconds, compared with 38
seconds (first load), 9.5 seconds (reload) and 9.7 seconds (save) before. That
is well under the roughly five seconds after which Windows marks a window as
"Not Responding".

## Blueprints and scanning (experimental)

Scanning an object with the Scan Tool records it in the save's
`GameProgress` → `Scanned` map, keyed by the object's `PrimaryAssetId`
(type `Item` plus a `DA_…` asset name) and holding a `VoyageScanData`
struct (`FirstScanTimeUtc`/`LastScanTimeUtc` in Unix seconds, `ScanCount`).
Blueprint pickups such as `DA_Prop_GyroBlueprintB`,
`DA_Prop_BulkPrototypeBlueprint`, and `DA_Prop_PortableContainerBlueprint`
are world props whose asset names contain `Blueprint` (or end in `BP`).
Memory drawings, quests, locations, and skills are not treated as blueprints.

**Mark Blueprints Scanned** lists every blueprint referenced in the save
that has no `Scanned` record and, after confirmation, appends one record per
missing blueprint (timestamped now, `ScanCount` 1). A backup is always made
first. The new entries use exactly the serialization the game writes (every
existing record rebuilds byte-for-byte), the map and every enclosing `Size`
header grow by the inserted bytes, and the map is re-parsed strictly before
anything is written. Whether the game unlocks the matching build from a
`Scanned` record alone is not yet confirmed. If the build does not appear,
or the save fails to load, restore the backup.

## Command line

```powershell
python last_caretaker_save_edit.py "<save.sav>" info
python last_caretaker_save_edit.py "<save.sav>" dump --out scalars.json
python last_caretaker_save_edit.py "<save.sav>" set --name Level --value 10
python last_caretaker_save_edit.py "<save.sav>" set --name ItemCount --value 999 --all
python last_caretaker_save_edit.py "<save.sav>" purge --dry-run
python last_caretaker_save_edit.py "<save.sav>" purge
python last_caretaker_save_edit.py "<save.sav>" purge --everywhere
python last_caretaker_save_edit.py "<save.sav>" blueprints
python last_caretaker_save_edit.py "<save.sav>" blueprints --mark-scanned
python last_caretaker_save_edit.py "<save.sav>" locks
python last_caretaker_save_edit.py "<save.sav>" locks --health off --energy off
```

`set` requires a unique match unless `--all` or a narrowing `--context` is
provided. `purge` permanently removes zero-count player inventory slots;
`--dry-run` only reports the count and `--everywhere` includes world containers.
CLI writes always create a backup, then use the same atomic-write and
round-trip verification path as the GUI.
