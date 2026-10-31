from __future__ import annotations

import struct
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import last_caretaker_save_edit as editor
import last_caretaker_save_gui as gui
import voyage_gvas as gvas


def _all_items_are_stacks(document):
    """Treat every fake item as a top-level stack (fakes carry no save bytes)."""
    document.raw = b""
    offsets = {item.entries["ItemCount"].offset for item in document.items}
    return patch.object(editor, "_item_map_reach", return_value=(offsets, []))


class InventoryTypeColorTests(unittest.TestCase):
    def test_existing_item_classifier_assigns_colors_even_with_generic_saved_categories(self):
        examples = (
            ("Ammo Rifle", "Item", "ammo"),
            ("Memory Toy", "Item", "memory"),
            ("Weapon GrenadeElectric", "Item", "weapons"),
            ("Food Protein", "Item", "food"),
            ("Iron", "Material", "materials"),
            ("Part Motor", "Item", "parts"),
            ("Cable Copper", "Item", "cables"),
            ("Iron Scrap", "Item", "resources"),
            ("Consumable Battery", "Item", "consumables"),
            ("Module Fabricator", "Equipment", "modules"),
            ("Cylinder Engine", "Item", "modules"),
            ("Back Battery", "Equipment", "equipment"),
            ("Paint", "Item", "paint"),
            ("Cartridge Javelin", "Ammo", "cartridges"),
            ("Prop Boom Box", "Item", "props"),
            ("Unrecognized item", "Item", "other"),
        )
        for asset, category, expected in examples:
            with self.subTest(asset=asset):
                item = editor.InventoryItem(0, asset, category, "", "", 0, {})
                self.assertEqual(f"inventory:{expected}", gui._inventory_type_tag(item))
                self.assertIn(expected, gui.INVENTORY_TYPE_COLORS)
        for key in editor.REPAIR_CATEGORY_KEYS:
            if key.startswith("storage.") or key == "not_carried":
                continue
            group = key.split(".", 1)[0]
            self.assertIn("modules" if group == "cylinders" else group,
                          gui.INVENTORY_TYPE_COLORS)

    def test_all_inventory_row_colors_meet_text_contrast_requirement(self):
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        foreground = luminance(gui.INVENTORY_ROW_FOREGROUND)
        for category, color in gui.INVENTORY_TYPE_COLORS.items():
            with self.subTest(category=category):
                background = luminance(color)
                self.assertGreaterEqual(
                    (max(background, foreground) + 0.05) / (min(background, foreground) + 0.05), 4.5,
                )
        self.assertNotEqual(gui.INVENTORY_TYPE_COLORS["ammo"], gui.INVENTORY_TYPE_COLORS["memory"])


class FrozenSettingsPathTests(unittest.TestCase):
    def test_frozen_build_keeps_settings_in_local_app_data(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(gui.sys, "frozen", True, create=True),
                patch.dict(gui.os.environ, {"LOCALAPPDATA": directory}),
            ):
                path = gui._settings_path()
        self.assertEqual(
            Path(directory) / "The Last Caretaker Save Editor" / "editor_settings.json",
            path,
        )

    def test_source_run_keeps_settings_beside_the_script(self):
        with patch.object(gui.sys, "frozen", False, create=True):
            self.assertEqual(gui.SCRIPT_DIR / "editor_settings.json", gui._settings_path())

    def test_settings_write_creates_the_frozen_settings_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "editor_settings.json"
            app = SimpleNamespace(_current_settings=lambda: {"repair_combine": True})
            with patch.object(gui, "SETTINGS_PATH", path):
                self.assertTrue(gui.LastCaretakerEditor._write_settings(app))
            self.assertEqual('{\n  "repair_combine": true\n}', path.read_text(encoding="utf-8"))


class SaveSelectionTests(unittest.TestCase):
    def test_open_browses_the_current_users_standard_save_folder(self):
        selected = Path("C:/Users/example/AppData/Local/Voyage/Saved/SaveGames/123/slot.sav")
        app = SimpleNamespace(_open_save=Mock())
        with patch.object(
            gui.filedialog, "askopenfilename", return_value=str(selected),
        ) as browse:
            gui.LastCaretakerEditor._open(app)
        self.assertEqual("Select a The Last Caretaker save", browse.call_args.kwargs["title"])
        self.assertEqual(editor.DEFAULT_SAVE_ROOT, browse.call_args.kwargs["initialdir"])
        app._open_save.assert_called_once_with(selected)

    def test_select_save_runs_user_open_actions(self):
        app = SimpleNamespace(save_var=SimpleNamespace(get=lambda: "selected.sav"), _open_save=Mock())
        gui.LastCaretakerEditor._select_save(app)
        app._open_save.assert_called_once_with(Path("selected.sav"))

    def test_refresh_keeps_the_discovered_full_path_and_newest_selection(self):
        newest = Path("C:/Users/example/AppData/Local/Voyage/Saved/SaveGames/123/newest.sav")
        older = Path("C:/Users/example/AppData/Local/Voyage/Saved/SaveGames/123/older.sav")
        combo = {}
        selected = []
        statuses = []
        app = SimpleNamespace(
            document=None,
            candidates=[],
            save_combo=combo,
            save_var=SimpleNamespace(set=selected.append),
            status_var=SimpleNamespace(set=statuses.append),
        )
        with patch.object(editor, "discover_saves", return_value=[newest, older]) as discover:
            gui.LastCaretakerEditor._refresh_saves(app)
        discover.assert_called_once_with()
        self.assertEqual([str(newest), str(older)], combo["values"])
        self.assertEqual([str(newest)], selected)
        self.assertEqual(["Found 2 save(s)"], statuses)


class CompressionMethodTests(unittest.TestCase):
    @staticmethod
    def _block(payload: bytes, raw_size: int, *, method: int = 2,
               chunk_size: int = editor.DEFAULT_CHUNK_SIZE) -> bytes:
        return (
            editor.CONTAINER_MAGIC
            + struct.pack("<QBQQQQ", chunk_size, method, len(payload), raw_size,
                          len(payload), raw_size)
            + payload
        )

    def test_oodle_block_uses_declared_uncompressed_size(self) -> None:
        raw = b"GVAS test payload"
        decoder = Mock(return_value=raw)
        with patch.dict(sys.modules, {"ooz": SimpleNamespace(decompress=decoder)}):
            decoded, chunk_size = editor.decompress_container(self._block(b"oodle", len(raw)))
        self.assertEqual(raw, decoded)
        self.assertEqual(editor.DEFAULT_CHUNK_SIZE, chunk_size)
        decoder.assert_called_once_with(b"oodle", len(raw))

    def test_mixed_oodle_and_zlib_blocks_repack_as_verified_zlib(self) -> None:
        first = b"GVAS" * 8
        last = b"partial"
        packed = self._block(b"oodle", len(first), chunk_size=len(first))
        packed += editor.compress_container(last, len(first))
        with patch.dict(sys.modules, {"ooz": SimpleNamespace(decompress=Mock(return_value=first))}):
            raw, chunk_size = editor.decompress_container(packed)
        self.assertEqual(first + last, raw)
        rebuilt = editor.compress_container(raw, chunk_size)
        self.assertEqual(3, rebuilt[16])
        with patch.dict(sys.modules, {"ooz": None}):
            self.assertEqual((raw, chunk_size), editor.decompress_container(rebuilt))

    def test_real_decoder_reads_stored_oodle_block(self) -> None:
        try:
            import ooz
        except ImportError:
            self.skipTest("Install requirements.txt to exercise the native Oodle decoder")
        raw = b"GVAS synthetic Oodle data"
        self.assertEqual(raw, ooz.decompress(b"\xcc\x06" + raw, len(raw)))
        self.assertEqual(
            (raw, editor.DEFAULT_CHUNK_SIZE),
            editor.decompress_container(self._block(b"\xcc\x06" + raw, len(raw))),
        )

    def test_missing_decoder_reports_installation_instructions(self) -> None:
        with patch.dict(sys.modules, {"ooz": None}):
            with self.assertRaisesRegex(editor.SaveFormatError, "pip install -r requirements.txt"):
                editor.decompress_container(self._block(b"oodle", 10))

    def test_decoder_failure_is_a_save_format_error(self) -> None:
        decoder = Mock(side_effect=RuntimeError("Could not decompress requested amount"))
        with patch.dict(sys.modules, {"ooz": SimpleNamespace(decompress=decoder)}):
            with self.assertRaisesRegex(editor.SaveFormatError, "Invalid Oodle payload in block 0"):
                editor.decompress_container(self._block(b"oodle", 10))

    def test_wrong_decoded_length_is_rejected(self) -> None:
        with patch.dict(sys.modules, {"ooz": SimpleNamespace(decompress=Mock(return_value=b"short"))}):
            with self.assertRaisesRegex(editor.SaveFormatError, "expands to 5 bytes, expected 10"):
                editor.decompress_container(self._block(b"oodle", 10))

    def test_invalid_sizes_and_truncation_are_rejected_before_decoding(self) -> None:
        decoder = Mock()
        cases = (
            self._block(b"oodle", 0),
            self._block(b"oodle", 11, chunk_size=10),
            self._block(b"oodle", 1, chunk_size=0),
            self._block(b"oodle", 10)[:-1],
        )
        with patch.dict(sys.modules, {"ooz": SimpleNamespace(decompress=decoder)}):
            for data in cases:
                with self.subTest(data=data):
                    with self.assertRaises(editor.SaveFormatError):
                        editor.decompress_container(data)
        decoder.assert_not_called()

    def test_unknown_compression_is_still_rejected(self) -> None:
        with self.assertRaisesRegex(editor.SaveFormatError, "Unsupported compression method 99"):
            editor.decompress_container(self._block(b"unknown", 10, method=99))

    def test_mixed_methods_still_require_consistent_chunk_sizes(self) -> None:
        packed = self._block(b"oodle", 10, chunk_size=10)
        packed += editor.compress_container(b"zlib", 20)
        with patch.dict(sys.modules, {"ooz": SimpleNamespace(decompress=Mock(return_value=b"x" * 10))}):
            with self.assertRaisesRegex(editor.SaveFormatError, "inconsistent chunk sizes"):
                editor.decompress_container(packed)


class ContainerTests(unittest.TestCase):
    def test_stack_targets_follow_item_rules(self) -> None:
        single = (
            "Weapon BoltGun", "Weapon ElectricPistol", "Weapon Flamethrower",
            "WaterJet", "HeadLight", "Nebula HeadLight", "DismantleTool", "RepairTool",
            "SeaMine Large", "Back Battery Large", "Prop Boom Box", "Memory Toy",
            "ItemLoot Teddy 01", "Weapon Petrol Grinder",
        )
        for asset in single:
            self.assertEqual(editor.SINGLE_STACK, editor.stack_target(asset, "Equipment"), asset)
        for asset in (
            "Binoculars", "ScanTool",
        ):
            self.assertEqual(editor.SINGLE_STACK, editor.stack_target(asset, "Equipment"), asset)
        for asset in ("Cartridge 100MDash", "Cartridge Javelin", "Cartridge Harpoon"):
            self.assertEqual(editor.CARTRIDGE_STACK, editor.stack_target(asset, "Ammo"), asset)
        for asset in ("Module Fabricator", "Module WindTurbine Small"):
            self.assertEqual(editor.MODULE_STACK, editor.stack_target(asset, "Item"), asset)
        module_five = (
            "Mooring Socket", "Paint ExoBuilt", "Weapon Attachment Laser",
        )
        for asset in module_five:
            self.assertEqual(editor.MODULE_STACK, editor.stack_target(asset, "Item"), asset)
        self.assertEqual(editor.PAINT_STACK, editor.stack_target("Paint", "Item"))
        for asset in ("DieselEngineCylinder", "ElectricEngineCylinder",
                      "DieselEngineCylinder Booster", "PetrolEngineCylinder SuperBooster",
                      "ElectricEngineCylinder Booster"):
            self.assertEqual(editor.CYLINDER_REPAIR_STACK, editor.stack_target(asset, "Item"), asset)
        self.assertEqual(editor.DEFAULT_STACK, editor.stack_target("Part StorageCylinder", "Item"))
        bulk = (
            ("Part PowerCore", "Item"), ("Cable Electricity", "Equipment"),
            ("Ammo Tracer", "Item"), ("Weapon Grenade Decoy", "Equipment"),
            ("Metal Scraps", "Item"),
            ("Iron", "Material"), ("SUModule BatterySmall", "Item"),
        )
        for asset, category in bulk:
            self.assertEqual(editor.DEFAULT_STACK, editor.stack_target(asset, category), asset)

    def test_is_bulk_resource_flags_the_heavy_categories(self) -> None:
        bulk = (
            ("Part PowerCore", "Item"), ("Cable Electricity", "Equipment"),
            ("Ammo Tracer", "Item"), ("Iron", "Material"),
        )
        for asset, category in bulk:
            self.assertTrue(editor.is_bulk_resource(asset, category), asset)
        not_bulk = (
            ("Binoculars", "Equipment"), ("Module Fabricator", "Item"),
            ("Weapon BoltGun", "Equipment"), ("SUModule BatterySmall", "Item"),
            ("Weapon Petrol Grinder", "Equipment"),
        )
        for asset, category in not_bulk:
            self.assertFalse(editor.is_bulk_resource(asset, category), asset)

    def test_repair_stack_target_tops_off_carried_bulk_to_999_and_forces_non_carried_to_1(self) -> None:
        # Carried bulk resources refill to the same 999 cap as manual editing.
        self.assertEqual(editor.DEFAULT_STACK, editor.repair_stack_target("Metal Scraps", "Item", True))
        self.assertEqual(editor.DEFAULT_STACK, editor.repair_stack_target("Iron", "Material", True))
        # Carried non-bulk rules (module/single) are unaffected.
        self.assertEqual(editor.MODULE_STACK, editor.repair_stack_target("Module Fabricator", "Item", True))
        self.assertEqual(editor.SINGLE_STACK, editor.repair_stack_target("Binoculars", "Equipment", True))
        self.assertEqual(editor.PAINT_STACK, editor.repair_stack_target("Paint", "Item", True))
        # Anything the player is not carrying is compacted to a stack of 1,
        # regardless of what its normal stack rule would be.
        self.assertEqual(editor.SINGLE_STACK, editor.repair_stack_target("Metal Scraps", "Item", False))
        self.assertEqual(editor.SINGLE_STACK, editor.repair_stack_target("Module Fabricator", "Item", False))
        # Special targets apply regardless of carried location.
        self.assertEqual(10, editor.repair_stack_target("Weapon Attachment Laser", "Equipment", True))
        self.assertEqual(10, editor.repair_stack_target("Weapon Attachment Laser", "Equipment", False))
        self.assertEqual(10, editor.repair_stack_target("Laser", "Weapon Attachment", False))
        self.assertEqual(2500, editor.repair_stack_target("Ammo Tracer", "Ammo", True))
        self.assertEqual(2500, editor.repair_stack_target("Ammo Tracer", "Ammo", False))
        self.assertEqual(2500, editor.repair_stack_target("Weapon GrenadePetrolBig", "Equipment", True))
        self.assertEqual(2500, editor.repair_stack_target("Weapon GrenadePetrolBig", "Equipment", False))
        for grenade in ("Weapon GrenadePetrol", "Weapon GrenadeOxygen", "Weapon GrenadeElectric", "Weapon GrenadeDecoy"):
            self.assertEqual(500, editor.repair_stack_target(grenade, "Equipment", True))
            self.assertEqual(500, editor.repair_stack_target(grenade, "Equipment", False))
        # Memory entries are repaired to 10 in every location. The standalone
        # duplicate-removal command protects them; Repair & Top Off's Combine
        # step opts into removing duplicate carried Memory stacks.
        self.assertEqual(10, editor.repair_stack_target("Memory Archive", "Memory", True))
        self.assertEqual(10, editor.repair_stack_target("Memory Archive", "Memory", False))

    def test_duplicate_protection_matches_asset_prefix_when_category_is_generic(self) -> None:
        def item(index: int, asset: str, category: str, count: int = 1):
            return editor.InventoryItem(
                index, asset, category, "Other", "Player Backpack", index, {
                    "ItemCount": editor.Entry(index, index, "ItemCount", "IntProperty", count),
                },
            )

        document = SimpleNamespace(items=[
            item(1, "Weapon BoltGun", "Equipment"),
            item(2, "Weapon BoltGun", "Equipment"),
            item(3, "Ammo Tracer", "Item", 5),
            item(4, "Ammo Tracer", "Item", 2),
            item(5, "Memory Toy", "Item"),
            item(6, "Memory Toy", "Item"),
            item(7, "Iron", "Material", 9),
            item(8, "Iron", "Material", 3),
        ])
        with _all_items_are_stacks(document):
            candidates = editor._all_duplicate_stack_candidates(document)
        self.assertEqual(["Iron"], [candidate.asset for candidate in candidates])
        with _all_items_are_stacks(document):
            repair_candidates = editor._all_duplicate_stack_candidates(
                document, include_memory=True)
            repair_duplicates = editor.find_duplicate_stacks(
                document, include_memory=True)
        self.assertEqual(["Iron", "Memory Toy"],
                         sorted(candidate.asset for candidate in repair_candidates))
        self.assertEqual(["Iron", "Memory Toy"],
                         sorted(candidate.asset for candidate in repair_duplicates))
        with _all_items_are_stacks(document):
            self.assertEqual(["Iron"], [
                candidate.asset for candidate in editor.find_duplicate_stacks(document)
            ])

    def test_repair_leaves_installed_attachment_counts_untouched(self) -> None:
        def item(index: int, asset: str, count: int):
            return editor.InventoryItem(
                index, asset, "Equipment", "Other", "Player Holster", index * 10, {
                    "ItemCount": editor.Entry(index, index * 10, "ItemCount", "IntProperty", count),
                },
            )

        installed = item(1, "Weapon Attachment Laser", 999)
        loose = item(2, "Weapon Attachment Laser", 999)
        document = SimpleNamespace(raw=b"", items=[installed, loose])
        with patch.object(editor, "installed_attachment_offsets", return_value={10}):
            edits = editor.repair_and_top_off(document, [installed, loose])
        self.assertEqual([(loose.entries["ItemCount"], 10)], edits)

    def test_duplicate_stack_rules_protect_ammo_memory_weapon_but_keep_explicit_grenade_deletes(self) -> None:
        def item(index: int, asset: str, category: str, actor: str = "Other", count: int = 1):
            return editor.InventoryItem(
                index, asset, category, actor, "Player Backpack", index, {
                    "ItemCount": editor.Entry(index, index, "ItemCount", "IntProperty", count),
                },
            )

        regular = [
            item(1, "Ammo Tracer", "Ammo", count=2),
            item(2, "Ammo Tracer", "Ammo", count=1),
            item(3, "Memory Archive", "Memory", count=2),
            item(4, "Memory Archive", "Memory", count=1),
            item(5, "Weapon BoltGun", "Weapon", count=1),
            item(6, "Weapon BoltGun", "Weapon", count=1),
            item(10, "Weapon Attachment Laser", "Equipment", count=2),
            item(11, "Weapon Attachment Laser", "Equipment", count=1),
            item(12, "Laser", "Weapon Attachment", count=2),
            item(13, "Laser", "Weapon Attachment", count=1),
        ]
        explicit = [
            item(7, "Weapon GrenadePetrol", "Equipment",
                 "BP_FirstPersonCharacter_New_C1"),
            item(8, "Weapon GrenadeOxygen", "Equipment",
                 "BP_FirstPersonCharacter_New_C1", count=7),
            item(9, "Weapon GrenadeElectric", "Equipment",
                 "BP_FirstPersonCharacter_New_C1"),
        ]
        document = SimpleNamespace(items=regular + explicit)
        with patch.object(editor, "_find_same_weapon_attachment_duplicates", return_value=[]), \
                _all_items_are_stacks(document):
            candidates = editor._all_duplicate_stack_candidates(document)
        self.assertEqual(
            {
                "Weapon GrenadePetrol",
                "Weapon GrenadeOxygen",
                "Weapon GrenadeElectric",
            },
            {candidate.asset for candidate in candidates},
        )

    def test_duplicate_grenade_stacks_are_removed_keeping_largest(self) -> None:
        def item(index: int, asset: str, count: int):
            return editor.InventoryItem(
                index, asset, "Equipment", "BP_FirstPersonCharacter_New_C1", "Player Backpack", index, {
                    "ItemCount": editor.Entry(index, index, "ItemCount", "IntProperty", count),
                },
            )

        petrol = [item(i, "Weapon GrenadePetrolBig", 2500 if i == 3 else 10) for i in range(1, 13)]
        others = [
            item(20, "Weapon Grenade Decoy", 500),
            item(21, "Weapon Grenade Decoy", 5),
            item(22, "Weapon BoltGun", 1),
            item(23, "Weapon BoltGun", 1),
        ]
        document = SimpleNamespace(items=petrol + others)
        with patch.object(editor, "_find_same_weapon_attachment_duplicates", return_value=[]), \
                _all_items_are_stacks(document):
            candidates = editor._all_duplicate_stack_candidates(document)
        removed_petrol = [c for c in candidates if c.asset == "Weapon GrenadePetrolBig"]
        self.assertEqual(11, len(removed_petrol))
        self.assertNotIn(3, {c.index for c in removed_petrol})
        self.assertEqual([21], [c.index for c in candidates if c.asset == "Weapon Grenade Decoy"])
        self.assertFalse([c for c in candidates if c.asset == "Weapon BoltGun"])

    def test_records_nested_inside_another_item_are_not_duplicate_stacks(self) -> None:
        def item(index: int, asset: str, count: int):
            return editor.InventoryItem(
                index, asset, "Equipment", "Other", "Player Backpack", index, {
                    "ItemCount": editor.Entry(index, index, "ItemCount", "IntProperty", count),
                },
            )

        # 1/2: real top-level stacks; 3/4: paints held inside a paint item;
        # 5: a nested copy larger than the real stack; 6: an unread map tail.
        items = [
            item(1, "Iron", 9), item(2, "Iron", 3),
            item(3, "Paint Holland", 0), item(4, "Paint Holland", 0),
            item(5, "Iron", 999), item(6, "Copper", 4), item(7, "Copper", 8),
        ]
        document = SimpleNamespace(raw=b"", items=items)
        reach = ({1, 2, 7}, [(6, 7)])
        with patch.object(editor, "_item_map_reach", return_value=reach), \
                patch.object(editor, "_find_same_weapon_attachment_duplicates", return_value=[]):
            candidates = editor._all_duplicate_stack_candidates(document)
            found = editor.find_duplicate_stacks(document)
            blocked = editor.count_unpurgeable_duplicate_stacks(document)
        self.assertEqual([2, 6], sorted(c.index for c in candidates))
        self.assertEqual([2], [c.index for c in found])
        self.assertEqual(1, blocked)

    def test_container_roundtrip_across_partial_chunk(self) -> None:
        raw = b"GVAS" + bytes(range(256)) * 1100
        packed = editor.compress_container(raw)
        unpacked, chunk_size = editor.decompress_container(packed)
        self.assertEqual(raw, unpacked)
        self.assertEqual(editor.DEFAULT_CHUNK_SIZE, chunk_size)

    def test_invalid_container_rejected(self) -> None:
        with self.assertRaises(editor.SaveFormatError):
            editor.decompress_container(b"not a save")

    def test_actual_save_can_be_loaded_and_repacked(self) -> None:
        saves = editor.discover_saves()
        if not saves:
            self.skipTest("No installed Last Caretaker save")
        document = editor.load_save(saves[0])
        self.assertGreater(len(document.entries), 100)
        self.assertGreater(len(document.items), 10)
        locations = {item.location for item in document.items}
        self.assertIn(editor.LOC_BACKPACK, locations)
        self.assertIn(editor.LOC_CATALOG, locations)
        # World containers and loose items come and go with play, so only
        # check their classification when the save currently has some.
        actors = {item.actor.casefold() for item in document.items}
        if any("inventory_container" in actor for actor in actors):
            self.assertIn("Container/Crate", locations)
        if any("dynamicmeshactor" in actor or "glowstick" in actor for actor in actors):
            self.assertIn("Environment", locations)
        player_stats = dict(editor.player_stat_entries(document))
        self.assertIn("Health - Current", player_stats)
        health = editor.player_survival_fields(document.raw)["Health"]
        self.assertEqual("MaxValue" in health, "Health - Maximum" in player_stats)
        enhancers = dict(editor.enhancer_points_entries(document))
        self.assertIn("Character HaulMastery", enhancers)
        self.assertEqual("IntProperty", enhancers["Character HaulMastery"].type)
        catalog = editor.player_item_catalog(document)
        self.assertGreater(len(catalog), 10)
        self.assertTrue(all(editor.is_carried(item.location)
                            for rows in catalog.values() for item in rows))
        rebuilt = editor.compress_container(document.raw, document.chunk_size)
        raw, chunk_size = editor.decompress_container(rebuilt)
        self.assertEqual(document.raw, raw)
        self.assertEqual(document.chunk_size, chunk_size)

    def test_edit_copy_creates_backup_and_preserves_structure(self) -> None:
        saves = editor.discover_saves()
        if not saves:
            self.skipTest("No installed Last Caretaker save")
        source = editor.load_save(saves[0])
        level = next(entry for entry in source.entries if entry.name == "Level")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            level = next(entry for entry in document.entries if entry.offset == level.offset)
            new_value = level.value + 1
            changed, backup = editor.apply_edits(document, [(level, new_value)])
            self.assertEqual(1, changed)
            self.assertIsNotNone(backup)
            self.assertTrue(backup.is_file())
            reloaded = editor.load_save(target)
            edited = next(entry for entry in reloaded.entries if entry.offset == level.offset)
            self.assertEqual(new_value, edited.value)
            self.assertEqual(len(document.raw), len(reloaded.raw))

    def test_item_location_classification(self) -> None:
        self.assertEqual(
            editor.LOC_BACKPACK,
            editor.classify_item_location("BP_FirstPersonCharacter_New_C1"),
        )
        self.assertEqual(
            "Container/Crate",
            editor.classify_item_location("BP_Inventory_Container_Buoy_Small_C1204"),
        )
        self.assertEqual(
            "Environment",
            editor.classify_item_location("BP_DynamicMeshActor_C10"),
        )
        self.assertEqual(
            editor.LOC_PLAYER_CRATE,
            editor.classify_item_location("BP_Inventory_PlayerBox_Large_C1204"),
        )
        self.assertTrue(editor.is_carried(editor.LOC_HOLSTER))
        self.assertTrue(editor.is_carried(editor.LOC_PLAYER_CRATE))
        self.assertFalse(editor.is_carried(editor.LOC_CATALOG))
        self.assertFalse(editor.is_carried(editor.LOC_MODULE))

    def test_player_components_are_separated(self) -> None:
        """The pawn's eight Items maps must not collapse into one location."""
        saves = editor.discover_saves()
        if not saves:
            self.skipTest("No installed Last Caretaker save")
        document = editor.load_save(saves[0])
        spans = editor.component_ranges(document.raw)
        self.assertGreater(len(spans), 1)
        labels = {location for _start, _end, location in spans}
        self.assertIn(editor.LOC_BACKPACK, labels)
        # The catalog holds one row per known asset, so every entry is unique.
        catalog = [item for item in document.items
                   if item.location == editor.LOC_CATALOG]
        self.assertEqual(len(catalog), len({item.anchor_offset for item in catalog}))


class MarkBlueprintsGuiTests(unittest.TestCase):
    def _app(self, events: list[str]):
        return SimpleNamespace(
            document=SimpleNamespace(path=Path("x.sav")),
            keep_var=SimpleNamespace(get=lambda: 5),
            status_var=SimpleNamespace(set=lambda value: events.append(f"status:{value}")),
            _structural_guard=lambda: True,
            _load=lambda path, *, confirm_discard: events.append("reload"),
        )

    def test_declined_confirmation_writes_nothing(self) -> None:
        events: list[str] = []
        app = self._app(events)
        with patch.object(gui.save_io, "blueprint_scan_status",
                          return_value=[("DA_Prop_GyroBlueprintB", False)]), \
                patch.object(gui.save_io, "mark_blueprints_scanned") as mark, \
                patch.object(gui.messagebox, "askyesno", return_value=False):
            gui.LastCaretakerEditor._mark_blueprints_scanned(app)
        mark.assert_not_called()
        self.assertEqual([], events)

    def test_confirmed_marks_missing_then_reloads(self) -> None:
        events: list[str] = []
        app = self._app(events)
        status = [("DA_Prop_GyroBlueprintB", False), ("DA_Prop_GyroBlueprintC", True)]
        with patch.object(gui.save_io, "blueprint_scan_status", return_value=status), \
                patch.object(gui.save_io, "mark_blueprints_scanned",
                             return_value=(["DA_Prop_GyroBlueprintB"], None)) as mark, \
                patch.object(gui.messagebox, "askyesno", return_value=True):
            gui.LastCaretakerEditor._mark_blueprints_scanned(app)
        mark.assert_called_once_with(
            app.document, ["DA_Prop_GyroBlueprintB"], make_backup=True, max_backups=5)
        self.assertEqual(["reload", "status:Marked 1 blueprint(s) scanned"], events)

    def test_all_scanned_reports_without_prompt(self) -> None:
        events: list[str] = []
        app = self._app(events)
        with patch.object(gui.save_io, "blueprint_scan_status",
                          return_value=[("DA_Prop_GyroBlueprintC", True)]), \
                patch.object(gui.messagebox, "askyesno") as ask:
            gui.LastCaretakerEditor._mark_blueprints_scanned(app)
        ask.assert_not_called()
        self.assertEqual(["status:All 1 known blueprint(s) are already scanned"], events)


class BusyUiTests(unittest.TestCase):
    @staticmethod
    def _app(events: list[str]):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value
                events.append(f"label:{value}")

        class Progress:
            def pack(self, **_kwargs):
                events.append("pack")

            def start(self, interval):
                events.append(f"start:{interval}")

            def step(self, amount):
                events.append(f"step:{amount}")

            def stop(self):
                events.append("stop")

            def pack_forget(self):
                events.append("hide")

        return SimpleNamespace(
            busy_var=Value(),
            busy_progress=Progress(),
            configure=lambda **kwargs: events.append(f"cursor:{kwargs['cursor']}"),
            update_idletasks=lambda: events.append("paint"),
        )

    def test_nested_busy_state_remains_visible_until_outer_operation_finishes(self) -> None:
        events: list[str] = []
        app = self._app(events)
        with gui._busy_ui(app, "Repair") as progress:
            progress("Saving")
            with gui._busy_ui(app, "Reloading"):
                self.assertEqual("Reloading", app.busy_var.value)
            self.assertEqual("Saving", app.busy_var.value)
            self.assertNotIn("stop", events)
        self.assertEqual("", app.busy_var.value)
        self.assertEqual(1, events.count("pack"))
        self.assertEqual(1, events.count("stop"))
        self.assertEqual(["cursor:watch", "cursor:"],
                         [event for event in events if event.startswith("cursor:")])

    def test_busy_state_is_cleared_after_an_exception(self) -> None:
        events: list[str] = []
        app = self._app(events)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with gui._busy_ui(app, "Working"):
                raise RuntimeError("failed")
        self.assertEqual("", app.busy_var.value)
        self.assertIn("stop", events)
        self.assertEqual("cursor:", events[-2])


class HumanSeedTimeTests(unittest.TestCase):
    def test_only_human_seed_timeleft_floats_are_changed(self) -> None:
        entries = [
            editor.Entry(0, 5, "TimeLeft", "FloatProperty", 1),
            editor.Entry(1, 20, "TimeLeft", "FloatProperty", 0),
            editor.Entry(2, 30, "TimeLeft", "FloatProperty", 99999),
            editor.Entry(3, 40, "TimeLeft", "DoubleProperty", 2),
            editor.Entry(4, 50, "Health", "FloatProperty", 3),
            editor.Entry(5, 70, "TimeLeft", "FloatProperty", 4),
            editor.Entry(6, 90, "TimeLeft", "FloatProperty", 5),
            editor.Entry(7, 110, "TimeLeft", "FloatProperty", 6),
        ]
        document = SimpleNamespace(entries=entries, anchors=[
            editor.TextAnchor(100, "ActorName", "BP_Item_HumanSeed_C_2"),
            editor.TextAnchor(10, "ActorName", "BP_Item_HumanSeed_C_1"),
            editor.TextAnchor(60, "ActorName", "BP_Item_HumanSeedling_C_1"),
            editor.TextAnchor(80, "ActorName", "BP_Item_Other_C_1"),
        ])
        self.assertEqual(
            [(entries[1], 99999.0), (entries[7], 99999.0)],
            editor.human_seed_time_edits(document),
        )

    def test_no_matching_timers_is_a_noop(self) -> None:
        self.assertEqual([], editor.human_seed_time_edits(
            SimpleNamespace(entries=[], anchors=[])))


class RocketResourceTests(unittest.TestCase):
    ROCKET = "BP_Rocket_Functional_C_UAID_C87F54CEF3306C4302_9d6ba793503a278e_1360151644"

    @staticmethod
    def _record(resource: str, actor: str, offset: int, value: float):
        entry = editor.Entry(0, offset, f"Resources.{resource}", "DoubleProperty", value)
        return editor.ResourceAmount(resource, actor, "", "Other", offset - 10, entry)

    def test_only_functional_rocket_targets_are_changed(self) -> None:
        records = [
            self._record("Electricity", self.ROCKET, 100, 12),
            self._record("Methane", self.ROCKET, 200, 0),
            self._record("Oxygen", self.ROCKET, 300, 5),
            self._record("Water", self.ROCKET, 400, 1),
            self._record("Electricity", "BP_Rocket_Broken_C_UAID_1", 500, 0),
            self._record("Oxygen", "BP_Module_Oxygen_Tank_C_UAID_2", 600, 0),
            self._record("Methane", "BP_NPC_Methane_C-1", 700, 0),
        ]
        edits = editor.rocket_resource_edits(SimpleNamespace(resources=records))
        self.assertEqual(
            [(records[0].entry, 60000), (records[1].entry, 3000), (records[2].entry, 12000)],
            edits,
        )
        self.assertTrue(all(not editor.is_refillable_storage(record) for record in records[:3]))

    def test_values_already_at_target_still_override_other_pending_edits(self) -> None:
        records = [
            self._record("Electricity", self.ROCKET, 100, 60000.0),
            self._record("Methane", self.ROCKET.lower(), 200, 3000.0),
            self._record("Oxygen", self.ROCKET, 300, 12000.0),
        ]
        self.assertEqual(
            [(r.entry, editor.ROCKET_RESOURCE_TARGETS[r.resource]) for r in records],
            editor.rocket_resource_edits(SimpleNamespace(resources=records)),
        )

    def test_configured_targets_and_storage_exclusion(self) -> None:
        records = [
            self._record(name, self.ROCKET, offset, 999999)
            for name, offset in (("Electricity", 100), ("Methane", 200), ("Oxygen", 300))
        ]
        other = self._record("Oxygen", "BP_Module_Oxygen_Tank_C_UAID_2", 400, 0)
        document = SimpleNamespace(resources=records + [other],
                                   entries=[r.entry for r in records + [other]])
        self.assertEqual([(other.entry, 999999)], editor.storage_refill_edits(document, 999999))
        self.assertEqual(
            [(records[0].entry, 100), (records[1].entry, 200), (records[2].entry, 300)],
            editor.rocket_resource_edits(document, {
                "rocket.electricity": 100, "rocket.methane": 200, "rocket.oxygen": 300,
            }),
        )

    def test_exact_rocket_amounts_survive_save_reload_with_storage_at_999999(self) -> None:
        fixture = ResourceAmountTests()
        actors = [
            (self.ROCKET, [(name, 999999.0) for name in editor.ROCKET_RESOURCE_TARGETS]),
            ("BP_Module_Battery_C-123", [("Electricity", 10.0)]),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rocket.sav"
            path.write_bytes(editor.compress_container(fixture._raw(actors)))
            document = editor.load_save(path)
            edits = editor.storage_refill_edits(document, 999999)
            edits.extend(editor.rocket_resource_edits(document))
            editor.apply_edits(document, edits, make_backup=False)
            reloaded = editor.load_save(path)
            self.assertEqual(
                [60000, 3000, 12000, 999999],
                [r.entry.value for r in reloaded.resources],
            )


class RepairAndTopOffGuiTests(unittest.TestCase):
    def _editor(
        self,
        events: list[str],
        *,
        combine: bool = True,
        save_succeeds: bool = True,
        refill: bool = False,
        rocket: bool = True,
    ):
        count_entry = SimpleNamespace(offset=10, value=1)
        item = SimpleNamespace(
            entries={"ItemCount": count_entry},
            count=1,
            location=editor.LOC_BACKPACK,
            asset="Iron",
        )
        app = SimpleNamespace(
            document=SimpleNamespace(items=[item], resources=[], entries=[], anchors=[]),
            pending={},
            status_var=SimpleNamespace(set=lambda _value: None),
            repair_combine_var=SimpleNamespace(get=lambda: combine),
            confirm_repair_var=SimpleNamespace(get=lambda: True),
            refill_storage_var=SimpleNamespace(get=lambda: refill),
            refill_rocket_var=SimpleNamespace(get=lambda: rocket),
            repair_targets={"ammo": 7},
            repair_asset_targets={},
            _selected_items=lambda: [item],
            _sync_document=lambda: events.append("sync") or True,
            _refresh_values=lambda: events.append("refresh"),
            _save=lambda *, confirm: events.append(f"save:{confirm}") or save_succeeds,
            _remove_duplicate_stacks=lambda *, confirm, include_memory=False:
                events.append(f"combine:{confirm}:{include_memory}"),
        )
        return app, item, count_entry

    def test_human_seed_only_changes_are_saved_without_storage_refill(self) -> None:
        events: list[str] = []
        app, _item, _entry = self._editor(events, combine=False, refill=False)
        timer = editor.Entry(0, 30, "TimeLeft", "FloatProperty", 12)
        app.document.items = []
        app.document.raw = b""
        app.document.entries = [timer]
        app.document.anchors = [
            editor.TextAnchor(20, "ActorName", "BP_Item_HumanSeed_C_123"),
        ]
        app._selected_items = lambda: []
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual({30: 99999.0}, app.pending)
        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_human_seed_timer_save_failure_stops_followup_actions(self) -> None:
        events: list[str] = []
        app, _item, _entry = self._editor(events, save_succeeds=False)
        timer = editor.Entry(0, 30, "TimeLeft", "FloatProperty", 12)
        app.document.entries = [timer]
        app.document.anchors = [
            editor.TextAnchor(20, "ActorName", "BP_Item_HumanSeed_C_123"),
        ]
        app.repair_delete_items = ["Iron"]
        app._remove_configured_items = lambda: events.append("delete")
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual({30: 99999.0}, app.pending)
        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_repair_saves_then_combines_when_enabled(self) -> None:
        events: list[str] = []
        app, _item, count_entry = self._editor(events)

        def askyesno(title: str, _message: str) -> bool:
            events.append(f"prompt:{title}")
            return True

        with (
            patch.object(gui.messagebox, "askyesno", side_effect=askyesno),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)

        self.assertEqual(
            [
                "sync", "prompt:Repair and top off?", "refresh", "save:False",
                "combine:False:True",
            ],
            events,
        )
        self.assertEqual({count_entry.offset: 999}, app.pending)

    def test_repair_stops_after_save_when_combine_is_disabled(self) -> None:
        events: list[str] = []
        app, _item, count_entry = self._editor(events, combine=False)

        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)

        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_repair_does_not_prompt_to_combine_when_automatic_save_fails(self) -> None:
        events: list[str] = []
        app, _item, count_entry = self._editor(events, save_succeeds=False)

        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)

        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_repair_passes_configured_targets(self) -> None:
        events: list[str] = []
        app, _item, count_entry = self._editor(events, combine=False)
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True) as prompt,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 7)]) as repair,
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        targets = repair.call_args.args[2]
        self.assertEqual(7, targets["ammo"])
        self.assertEqual(editor.default_repair_targets()["memory"], targets["memory"])
        self.assertIn("Ammo 7", prompt.call_args.args[1])

    def test_repair_warning_can_be_disabled(self):
        events = []
        app, _, entry = self._editor(events)
        app.confirm_repair_var = SimpleNamespace(get=lambda: False)
        with (
            patch.object(gui.messagebox, "askyesno") as prompt,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        prompt.assert_not_called()
        self.assertIn("save:False", events)

    def test_storage_refill_is_queued_before_save_and_includes_npc_amounts(self) -> None:
        events = []
        app, _, count_entry = self._editor(events, refill=True, save_succeeds=False)
        app.repair_targets["storage.tanks"] = 31000
        app.document.resources = [
            editor.ResourceAmount("Oil", "BP_Module_Oil_Tank_C-87903055", "", "Other", 20,
                                  editor.Entry(1, 100, "Resources.Oil", "DoubleProperty", 0),
                                  editor.Entry(3, 300, "Health", "FloatProperty", 200),
                                  editor.Entry(4, 400, "ExtraDamage", "FloatProperty", 0)),
            editor.ResourceAmount("Electricity", "BP_NPC_Shark_C-88040454", "", "Other", 30,
                                  editor.Entry(2, 200, "Resources.Electricity", "DoubleProperty", 100)),
        ]
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual({10: 999, 100: 31000, 200: 31000, 300: 10000, 400: 10000}, app.pending)
        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_storage_refill_disabled_leaves_resources_unchanged(self) -> None:
        app, _, entry = self._editor([], refill=False)
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "storage_refill_edits") as refill,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        refill.assert_not_called()

    def test_rocket_targets_override_storage_and_other_pending_edits(self) -> None:
        app, _, entry = self._editor([], refill=True, combine=False)
        app.repair_targets["storage.tanks"] = 999999
        app.document.resources = [
            RocketResourceTests._record(name, RocketResourceTests.ROCKET, offset, value)
            for name, offset, value in (
                ("Electricity", 100, 999999), ("Methane", 200, 3000), ("Oxygen", 300, 999999),
            )
        ]
        app.document.entries = [r.entry for r in app.document.resources]
        app.pending[200] = 999999
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[
                (entry, 999), (app.document.resources[0].entry, 999999),
            ]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual(60000, app.pending[100])
        self.assertNotIn(200, app.pending)
        self.assertEqual(12000, app.pending[300])

    def test_disabled_rocket_top_off_is_not_refilled_by_storage(self) -> None:
        app, _, entry = self._editor([], refill=True, rocket=False, combine=False)
        resource = RocketResourceTests._record("Methane", RocketResourceTests.ROCKET, 100, 9)
        app.document.resources = [resource]
        app.document.entries = [resource.entry]
        app.repair_targets["storage.tanks"] = 999999
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "rocket_resource_edits") as rocket,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        rocket.assert_not_called()
        self.assertEqual({10: 999}, app.pending)

    def test_custom_rocket_amount_is_used(self) -> None:
        app, _, entry = self._editor([], refill=True, combine=False)
        app.repair_targets["rocket.methane"] = 2500
        app.document.resources = [
            RocketResourceTests._record("Methane", RocketResourceTests.ROCKET, 100, 999999),
        ]
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual(2500, app.pending[100])

    def test_storage_only_save_can_refill_without_carried_items(self) -> None:
        app, _, _ = self._editor([], refill=True, combine=False)
        app.document.items = []
        app._selected_items = lambda: []
        entry = editor.Entry(0, 100, "Resources.Petrol", "DoubleProperty", 0)
        app.document.resources = [
            editor.ResourceAmount("Petrol", "BP_Module_Petrol_Container_C-88003045",
                                  "", "Other", 20, entry),
        ]
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual({100: 50000}, app.pending)

    def test_rocket_resources_are_saved_without_storage_refill(self) -> None:
        events: list[str] = []
        app, _, _ = self._editor(events, refill=False, combine=False)
        app.document.items = []
        app._selected_items = lambda: []
        rocket = "BP_Rocket_Functional_C_UAID_C87F54CEF3306C4302_9d6ba793503a278e_1360151644"
        app.document.resources = [
            editor.ResourceAmount(name, rocket, "", "Other", offset - 10,
                                  editor.Entry(0, offset, f"Resources.{name}", "DoubleProperty", 0))
            for name, offset in (("Electricity", 100), ("Methane", 200), ("Oxygen", 300))
        ]
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True) as prompt,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual({100: 60000, 200: 3000, 300: 12000}, app.pending)
        self.assertEqual(["sync", "refresh", "save:False"], events)
        self.assertIn("Set 3 Functional Rocket resource field(s): Electricity 60000, "
                      "Methane 3000, Oxygen 12000", prompt.call_args.args[1])


class PurgeWarningGuiTests(unittest.TestCase):
    def test_purge_confirmation_enabled_disabled_and_declined(self):
        from unittest.mock import Mock
        for warn, approved, expected_calls in ((True, True, 1), (True, False, 0), (False, False, 1)):
            with self.subTest(warn=warn, approved=approved):
                app = SimpleNamespace(
                    document=SimpleNamespace(), _structural_guard=lambda: True,
                    purge_include_world=SimpleNamespace(get=lambda: True),
                    confirm_purge_var=SimpleNamespace(get=lambda: warn),
                    keep_var=SimpleNamespace(get=lambda: 20),
                    _purge=Mock(),
                )
                with (
                    patch.object(gui.save_io, "count_empty_slots", return_value=3),
                    patch.object(gui.messagebox, "askyesno", return_value=approved) as prompt,
                ):
                    gui.LastCaretakerEditor._purge_empty_slots(app)
                self.assertEqual(int(warn), prompt.call_count)
                self.assertEqual(expected_calls, app._purge.call_count)

    def test_disabling_warning_does_not_bypass_structural_guard(self):
        from unittest.mock import Mock
        app = SimpleNamespace(_structural_guard=lambda: False, _purge=Mock())
        gui.LastCaretakerEditor._purge_empty_slots(app)
        app._purge.assert_not_called()


class DeleteShortcutGuiTests(unittest.TestCase):
    @staticmethod
    def _app(counts: tuple[int, ...]):
        items = []
        for offset, count in enumerate(counts, start=10):
            entry = SimpleNamespace(offset=offset, value=count)
            items.append(SimpleNamespace(entries={"ItemCount": entry}))
        statuses: list[str] = []
        app = SimpleNamespace(
            pending={},
            confirm_delete_var=SimpleNamespace(get=lambda: True),
            _selected_items=lambda: items,
            _refresh_values=lambda: None,
            status_var=SimpleNamespace(set=statuses.append),
        )
        app._delete_items = lambda: gui.LastCaretakerEditor._delete_items(app)
        return app, items, statuses

    def test_delete_key_queues_single_or_multiple_selected_items(self) -> None:
        for counts in ((7,), (3, 8, 12)):
            with self.subTest(selected=len(counts)):
                app, items, statuses = self._app(counts)
                with patch.object(gui.messagebox, "askyesno", return_value=True) as confirm:
                    result = gui.LastCaretakerEditor._delete_items_event(app)
                self.assertEqual("break", result)
                self.assertEqual(
                    {item.entries["ItemCount"].offset: 0 for item in items},
                    app.pending,
                )
                self.assertIn(
                    f"Set the count of {len(items)} selected item record(s) to zero?",
                    confirm.call_args.args[1],
                )
                self.assertEqual(
                    f"Queued deletion of {len(items)} item record(s)",
                    statuses[-1],
                )

    def test_delete_key_honors_confirmation(self) -> None:
        app, _items, statuses = self._app((4, 5))
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            result = gui.LastCaretakerEditor._delete_items_event(app)
        self.assertEqual("break", result)
        self.assertEqual({}, app.pending)
        self.assertEqual([], statuses)


class RepairTargetConfigTests(unittest.TestCase):
    def test_charge_injector_has_its_own_configurable_target(self) -> None:
        key = "consumables.charge_injector"
        self.assertIn((key, ("Consumable ChargeInjector",), 500, False), editor.REPAIR_CATEGORIES)
        self.assertEqual(500, editor.normalize_repair_targets({"consumables": 999})[key])
        for asset, category in (
            ("Consumable ChargeInjector", "Item"),
            (" consumable chargeinjector ", "Material"),
        ):
            with self.subTest(asset=asset):
                self.assertEqual(key, editor.repair_category(asset, category))
                self.assertEqual(500, editor.repair_stack_target(asset, category, True))
                self.assertEqual(500, editor.repair_stack_target(
                    asset, category, True, {"consumables": 9999, "other": 9999}))
                self.assertEqual(750, editor.repair_stack_target(asset, category, True, {key: 750}))
                self.assertEqual(12, editor.repair_stack_target(
                    asset, category, True, {key: 750}, {"Consumable ChargeInjector": 12}))
                self.assertEqual(1, editor.repair_stack_target(asset, category, False))
        self.assertEqual("consumables", editor.repair_category("Consumable ChargeInjectorLarge", "Item"))
        self.assertEqual(999, editor.repair_stack_target("Consumable BatteryLarge", "Item", True))
        entry = editor.Entry(0, 10, "ItemCount", "IntProperty", 2)
        item = editor.InventoryItem(
            0, "Consumable ChargeInjector", "Item", "Pawn", editor.LOC_BACKPACK, 5, {"ItemCount": entry})
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual([(entry, 500)], editor.repair_and_top_off(
                SimpleNamespace(raw=b"", items=[item]), [item]))
            entry.value = 0
            self.assertEqual([], editor.repair_and_top_off(
                SimpleNamespace(raw=b"", items=[item]), [item]))

    def test_food_target_is_configurable_and_preserves_scope_and_overrides(self) -> None:
        self.assertEqual(999, editor.default_repair_targets()["food"])
        self.assertIn(("food", ("Food",), 999, False), editor.REPAIR_CATEGORIES)
        for asset, category in (
            ("Food Ration", "Item"), ("Food_Algae", "Item"), ("Meal", "Food"),
        ):
            with self.subTest(asset=asset):
                self.assertEqual("food", editor.repair_category(asset, category))
                self.assertEqual(75, editor.repair_stack_target(asset, category, True, {"food": 75}))
                self.assertEqual(1, editor.repair_stack_target(asset, category, False, {"food": 75}))
                self.assertEqual(8, editor.repair_stack_target(
                    asset, category, True, {"food": 75}, {asset: 8}))
        self.assertEqual("other", editor.repair_category("FoodProcessor", "Item"))
        self.assertEqual(75, editor.normalize_repair_targets({"food": "75"})["food"])
        entry = editor.Entry(0, 10, "ItemCount", "IntProperty", 2)
        item = editor.InventoryItem(0, "Food Ration", "Item", "Pawn",
                                    editor.LOC_BACKPACK, 5, {"ItemCount": entry})
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual([(entry, 75)], editor.repair_and_top_off(
                SimpleNamespace(raw=b"", items=[item]), [item], {"food": 75}))

    def test_v6_resource_families_have_configurable_categories(self) -> None:
        cases = {
            ("Rubber Scraps", "Item"): "resources.scraps",
            ("Cash Fabric Scraps", "Item"): "resources.scraps",
            ("Ultra Tech Scrap", "Item"): "resources.scraps",
            ("Chromite Nugget", "Item"): "resources.nuggets",
            ("Pigment Cobalt Aluminate Blue", "Item"): "resources.pigments",
            ("Bio SeaWeed", "Item"): "resources.bio",
            ("Consumable BatteryLarge", "Equipment"): "consumables",
            ("Consumable Battery", "Material"): "materials",
            ("Biomass Reactor", "Item"): "other",
            ("TreeFruit Pear", "Item"): "other",
        }
        defaults = editor.default_repair_targets()
        for (asset, category), key in cases.items():
            with self.subTest(asset=asset):
                self.assertEqual(key, editor.repair_category(asset, category))
                self.assertEqual(999, defaults[key])
                self.assertEqual(1, editor.repair_stack_target(asset, category, False, {key: 40}))
                if key not in ("materials", "other"):
                    self.assertEqual(40, editor.repair_stack_target(asset, category, True, {key: 40}))
                    self.assertEqual(999, editor.repair_stack_target(asset, category, True, {"other": 40}))

    def test_categories_cover_weapon_subcategories(self) -> None:
        cases = {
            ("Weapon BoltGun", "Equipment"): "weapons.guns",
            ("Weapon GrenadePetrolBig", "Equipment"): "weapons.grenades.big_petrol",
            ("Weapon GrenadeOxygen", "Equipment"): "weapons.grenades.other",
            ("Weapon Attachment Laser", "Equipment"): "weapons.attachments",
            ("Ammo Tracer", "Item"): "ammo",
            ("Cartridge Javelin", "Ammo"): "cartridges",
            ("Memory Toy", "Item"): "memory",
            ("Module Enhancer Flash Array", "Item"): "modules.enhancers",
            ("PetrolEngineCylinder Booster", "Item"): "cylinders",
            ("DieselEngineCylinder", "Item"): "cylinders",
            ("ItemLoot Trash Metal Cylinder 01", "Item"): "cylinders",
            ("Part StorageCylinder", "Material"): "materials",
            ("Equipment Glowstick", "Equipment"): "equipment.glow",
            ("Paint", "Item"): "paint.paint",
            ("Paint Dragon", "Item"): "paint.designs",
            ("Metal Scraps", "Item"): "resources.scraps",
            ("Trash CardboardBoxA 01", "Item"): "other",
        }
        for (asset, category), key in cases.items():
            self.assertEqual(key, editor.repair_category(asset, category), asset)

    def test_glow_items_use_the_glow_target(self) -> None:
        for asset in ("Equipment DepthGlow", "Equipment Glowstick"):
            self.assertEqual("equipment.glow", editor.repair_category(asset, "Equipment"), asset)
            self.assertEqual(100, editor.repair_stack_target(asset, "Equipment", True), asset)
            self.assertEqual(100, editor.stack_target(asset, "Equipment"), asset)
        self.assertEqual("equipment.tools", editor.repair_category("Equipment Waterjet", "Equipment"))
        self.assertEqual(1, editor.repair_stack_target("Equipment DepthGlow", "Equipment", False))

    def test_nightvision_tops_off_to_one_without_changing_normal_stack_target(self) -> None:
        entry = editor.Entry(0, 10, "ItemCount", "IntProperty", 5)
        item = editor.InventoryItem(
            0, "Nightvision", "Item", "Pawn", editor.LOC_BACKPACK, 5, {"ItemCount": entry})
        self.assertEqual("equipment.tools", editor.repair_category("Nightvision", "Item"))
        self.assertEqual(1, editor.repair_stack_target("Nightvision", "Item", True))
        self.assertEqual(999, editor.stack_target("Nightvision", "Item"))
        document = SimpleNamespace(raw=b"", items=[item])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual([(entry, 1)], editor.repair_and_top_off(document, [item]))

    def test_cylinders_use_the_cylinder_target_except_storage_cylinder(self) -> None:
        for asset in ("DieselEngineCylinder", "PetrolEngineCylinder SuperBooster",
                      "ItemLoot Trash Metal Cylinder 03", "Storage Cylinder"):
            self.assertEqual("cylinders", editor.repair_category(asset, "Item"), asset)
            self.assertEqual(50, editor.repair_stack_target(asset, "Item", True), asset)
        self.assertEqual("materials", editor.repair_category("Part StorageCylinder", "Material"))
        self.assertEqual("parts", editor.repair_category("Part StorageCylinder", "Item"))
        self.assertEqual(50, editor.stack_target("DieselEngineCylinder"))
        self.assertEqual(1, editor.repair_stack_target("DieselEngineCylinder", "Item", False))

    def test_cable_named_items_are_cables_even_with_material_category(self) -> None:
        for asset, category in (
            ("Cable Electricity", "Material"),
            ("Cable Data", "Item"),
            ("Power Cable", "Item"),
        ):
            self.assertEqual("cables", editor.repair_category(asset, category), asset)
            self.assertEqual(100, editor.repair_stack_target(asset, category, True), asset)
        self.assertEqual("materials", editor.repair_category("Aluminium", "Material"))

    def test_memory_prefix_gets_memory_target_even_with_generic_category(self) -> None:
        self.assertEqual(10, editor.repair_stack_target("Memory Toy", "Item", True))
        self.assertEqual(10, editor.repair_stack_target("Memory Toy", "Item", False))

    def test_configured_targets_override_defaults_and_scope(self) -> None:
        targets = {"ammo": 50, "weapons.grenades.other": 42, "modules.modules": 3, "not_carried": 2}
        self.assertEqual(50, editor.repair_stack_target("Ammo Tracer", "Ammo", False, targets))
        self.assertEqual(42, editor.repair_stack_target("Weapon GrenadeOxygen", "Equipment", True, targets))
        self.assertEqual(3, editor.repair_stack_target("Module Fabricator", "Item", True, targets))
        self.assertEqual(2, editor.repair_stack_target("Module Fabricator", "Item", False, targets))
        self.assertEqual(999, editor.repair_stack_target("Iron", "Material", True, targets))

    def test_all_cartridges_use_ten_even_when_save_category_is_ammo(self) -> None:
        for asset in (
            "Cartridge 100MDash",
            "Cartridge Javelin",
            "Cartridge Harpoon",
        ):
            self.assertEqual("cartridges", editor.repair_category(asset, "Ammo"), asset)
            self.assertEqual(10, editor.repair_stack_target(asset, "Ammo", True), asset)
            self.assertEqual(10, editor.repair_stack_target(asset, "Ammo", False), asset)
            self.assertEqual(10, editor.stack_target(asset, "Ammo"), asset)

    def test_repair_reclassifies_javelin_before_applying_ammo_target(self) -> None:
        entry = editor.Entry(1, 10, "ItemCount", "IntProperty", 2500)
        item = editor.InventoryItem(
            1, "Cartridge Javelin", "Ammo", "Other", editor.LOC_BACKPACK, 10,
            {"ItemCount": entry},
        )
        document = SimpleNamespace(raw=b"", items=[item])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual([(entry, 10)], editor.repair_and_top_off(
                document, [item], {"ammo": 2500, "cartridges": 10}))

    def test_normalize_drops_invalid_values(self) -> None:
        defaults = editor.default_repair_targets()
        self.assertEqual(defaults, editor.normalize_repair_targets(None))
        self.assertEqual(defaults, editor.normalize_repair_targets(["ammo"]))
        result = editor.normalize_repair_targets(
            {"ammo": 0, "memory": "x", "paint.paint": True, "bogus": 5, "parts": "12"})
        self.assertEqual({**defaults, "parts": 12}, result)

    def test_repair_and_top_off_uses_targets(self) -> None:
        entry = editor.Entry(1, 10, "ItemCount", "IntProperty", 3)
        item = editor.InventoryItem(1, "Ammo Tracer", "Ammo", "Other", "Player Backpack", 10, {"ItemCount": entry})
        document = SimpleNamespace(raw=b"", items=[item])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual([(entry, 77)], editor.repair_and_top_off(document, [item], {"ammo": 77}))

    def test_gui_apply_validates_and_persists(self) -> None:
        writes: list[str] = []
        app = SimpleNamespace(repair_targets=editor.default_repair_targets(),
                              _write_settings=lambda: writes.append("write") or True,
                              _repair_label=gui.LastCaretakerEditor._repair_label)
        values = {key: str(value) for key, value in editor.default_repair_targets().items()}
        values["ammo"] = "1234"
        values["food"] = "75"
        values["consumables.charge_injector"] = "500"
        applied = gui.LastCaretakerEditor._apply_repair_targets(app, values)
        self.assertEqual(1234, applied["ammo"])
        self.assertEqual(1234, app.repair_targets["ammo"])
        self.assertEqual(75, applied["food"])
        self.assertEqual(75, app.repair_targets["food"])
        self.assertEqual(500, app.repair_targets["consumables.charge_injector"])
        self.assertEqual(["write"], writes)
        for bad in ("0", "abc", str(editor.MAX_STACK_TARGET + 1)):
            with self.assertRaises(ValueError):
                gui.LastCaretakerEditor._apply_repair_targets(app, {**values, "ammo": bad})
        self.assertEqual(["write"], writes)
        self.assertEqual(1234, app.repair_targets["ammo"])


class AssetStackTargetTests(unittest.TestCase):
    def test_normalize_trims_sorts_and_drops_invalid_rows(self) -> None:
        result = editor.normalize_asset_targets(
            {"  Bio Flesh  ": "5000", "Aluminium": 2500, "": 5, "bad": "x",
             "zero": 0, "flag": True, 7: 5, "huge": editor.MAX_STACK_TARGET + 1})
        self.assertEqual({"Aluminium": 2500, "Bio Flesh": 5000}, result)
        self.assertEqual(["Aluminium", "Bio Flesh"], list(result))
        self.assertEqual({}, editor.normalize_asset_targets(None))
        self.assertEqual({}, editor.normalize_asset_targets(["Iron"]))

    def test_asset_override_beats_category_and_is_case_insensitive(self) -> None:
        overrides = {"Bio Flesh": 5000, "Weapon Grenade Decoy": 500}
        self.assertEqual(5000, editor.repair_stack_target("Bio Flesh", "Item", True, None, overrides))
        self.assertEqual(5000, editor.repair_stack_target("bio flesh", "Item", True, None, overrides))
        self.assertEqual(
            500, editor.repair_stack_target("Weapon Grenade Decoy", "Equipment", True, None, overrides))
        self.assertEqual(999, editor.repair_stack_target("Aluminium", "Material", True, None, overrides))

    def test_override_never_revives_non_carried_ordinary_records(self) -> None:
        overrides = {"Bio Flesh": 5000, "Ammo Tracer": 77}
        self.assertEqual(1, editor.repair_stack_target("Bio Flesh", "Item", False, None, overrides))
        self.assertEqual(77, editor.repair_stack_target("Ammo Tracer", "Ammo", False, None, overrides))

    def test_repair_and_top_off_applies_asset_overrides(self) -> None:
        entry = editor.Entry(1, 10, "ItemCount", "IntProperty", 3)
        item = editor.InventoryItem(
            1, "Bio Flesh", "Item", "Other", "Player Backpack", 10, {"ItemCount": entry})
        document = SimpleNamespace(raw=b"", items=[item])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual(
                [(entry, 5000)],
                editor.repair_and_top_off(document, [item], None, {"Bio Flesh": 5000}),
            )

    def test_capture_uses_largest_carried_stack_and_skips_installed_attachments(self) -> None:
        def make(asset: str, location: str, count: int, offset: int):
            entry = editor.Entry(offset, offset, "ItemCount", "IntProperty", count)
            return editor.InventoryItem(
                offset, asset, "Item", "Other", location, offset, {"ItemCount": entry})

        items = [
            make("Bio Flesh", editor.LOC_BACKPACK, 2500, 10),
            make("Bio Flesh", editor.LOC_BACKPACK, 5000, 20),
            make("Iron", "World", 999, 30),
            make("Empty", editor.LOC_BACKPACK, 0, 40),
            make("Weapon Attachment Laser", editor.LOC_BACKPACK, 1, 50),
        ]
        document = SimpleNamespace(raw=b"", items=items)
        with patch.object(editor, "installed_attachment_offsets", return_value={50}):
            captured = editor.asset_targets_from_document(document)
        self.assertEqual({"Bio Flesh": 5000}, captured)

    def test_gui_apply_and_capture_persist_asset_targets(self) -> None:
        writes: list[str] = []
        app = SimpleNamespace(
            repair_targets=editor.default_repair_targets(),
            repair_asset_targets={},
            document=SimpleNamespace(raw=b"", items=[]),
            status_var=SimpleNamespace(set=lambda _value: None),
            _write_settings=lambda: writes.append("write") or True,
            _repair_label=gui.LastCaretakerEditor._repair_label,
        )
        values = {key: str(value) for key, value in editor.default_repair_targets().items()}
        gui.LastCaretakerEditor._apply_repair_targets(
            app, values, {"Bio Flesh": "5000", "Dropped": "  "})
        self.assertEqual({"Bio Flesh": 5000}, app.repair_asset_targets)
        with self.assertRaises(ValueError):
            gui.LastCaretakerEditor._apply_repair_targets(app, values, {"Bio Flesh": "nope"})
        self.assertEqual({"Bio Flesh": 5000}, app.repair_asset_targets)

        with patch.object(gui.save_io, "asset_targets_from_document", return_value={"Iron": 42}):
            self.assertEqual(1, gui.LastCaretakerEditor._capture_asset_targets(app))
        self.assertEqual({"Iron": 42}, app.repair_asset_targets)
        gui.LastCaretakerEditor._clear_asset_targets(app)
        self.assertEqual({}, app.repair_asset_targets)

    def test_gui_repair_passes_asset_overrides(self) -> None:
        events: list[str] = []
        count_entry = SimpleNamespace(offset=10, value=1)
        item = SimpleNamespace(
            entries={"ItemCount": count_entry}, count=1,
            location=editor.LOC_BACKPACK, asset="Bio Flesh")
        app = SimpleNamespace(
            document=SimpleNamespace(items=[item], resources=[], entries=[], anchors=[]),
            pending={},
            status_var=SimpleNamespace(set=lambda _value: None),
            repair_combine_var=SimpleNamespace(get=lambda: False),
            repair_targets={},
            repair_asset_targets={"Bio Flesh": 5000},
            confirm_repair_var=SimpleNamespace(get=lambda: True),
            refill_storage_var=SimpleNamespace(get=lambda: False),
            refill_rocket_var=SimpleNamespace(get=lambda: True),
            _selected_items=lambda: [item],
            _sync_document=lambda: True,
            _refresh_values=lambda: events.append("refresh"),
            _save=lambda *, confirm: events.append(f"save:{confirm}") or True,
            _remove_duplicate_stacks=lambda *, confirm, include_memory=False:
                events.append("combine"),
        )
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True) as prompt,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 5000)]) as repair,
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual({"Bio Flesh": 5000}, repair.call_args.args[3])
        self.assertIn("1 per-item stack size(s)", prompt.call_args.args[1])
        self.assertEqual(["refresh", "save:False"], events)


class DeletionItemsTests(unittest.TestCase):
    def _item(self, asset: str, location: str, count: int, offset: int):
        entry = editor.Entry(offset, offset, "ItemCount", "IntProperty", count)
        return editor.InventoryItem(
            offset, asset, "Item", "Other", location, offset, {"ItemCount": entry})

    def test_repair_tops_off_trash_and_scrap_without_zeroing(self) -> None:
        scrap = self._item("Metal Scraps", editor.LOC_BACKPACK, 2500, 10)
        trash = self._item("ItemLoot Trash Metal Cylinder", editor.LOC_BACKPACK, 50, 15)
        keep = self._item("Aluminium", editor.LOC_BACKPACK, 12, 20)
        document = SimpleNamespace(raw=b"", items=[scrap, trash, keep])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            edits = editor.repair_and_top_off(document, [scrap, trash, keep])
        self.assertEqual(
            [
                (scrap.entries["ItemCount"], editor.repair_stack_target(scrap.asset, "Item", True)),
                (keep.entries["ItemCount"], 999),
            ],
            edits,
        )

    def test_all_bio_items_are_kept_and_topped_off(self) -> None:
        organic = self._item("Bio Organic", editor.LOC_BACKPACK, 500, 10)
        waste = self._item("Bio Waste", editor.LOC_BACKPACK, 200, 15)
        flesh = self._item("Bio Flesh", editor.LOC_BACKPACK, 3000, 20)
        document = SimpleNamespace(raw=b"", items=[organic, waste, flesh])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            edits = editor.repair_and_top_off(document, [organic, waste, flesh])
        self.assertEqual(
            [
                (organic.entries["ItemCount"], 999),
                (waste.entries["ItemCount"], 999),
                (flesh.entries["ItemCount"], 999),
            ],
            edits,
        )
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            found = editor.deletion_records(document, [])
        self.assertEqual([], found)

    def test_asset_override_applies_to_scraps(self) -> None:
        scrap = self._item("Metal Scraps", editor.LOC_BACKPACK, 2500, 10)
        document = SimpleNamespace(raw=b"", items=[scrap])
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            edits = editor.repair_and_top_off(
                document, [scrap], None, {"Metal Scraps": 2500})
        self.assertEqual([], edits)

    def test_capture_includes_scraps_and_trash(self) -> None:
        items = [
            self._item("Metal Scraps", editor.LOC_BACKPACK, 2500, 10),
            self._item("Scrap Metal", editor.LOC_BACKPACK, 2500, 15),
            self._item("Trash Metal", editor.LOC_BACKPACK, 2500, 20),
            self._item("Aluminium", editor.LOC_BACKPACK, 2500, 30),
        ]
        document = SimpleNamespace(raw=b"", items=items)
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            self.assertEqual(
                {item.asset: 2500 for item in items}, editor.asset_targets_from_document(document))

    def test_deletion_records_match_exact_configured_names_and_only_carried_items(self) -> None:
        items = [
            self._item("Metal Scraps", editor.LOC_BACKPACK, 2500, 10),
            self._item("Trash Metal", editor.LOC_BACKPACK, 2500, 15),
            self._item("Glass Scrap", "World", 2500, 20),
            self._item("Aluminium", editor.LOC_BACKPACK, 2500, 30),
        ]
        document = SimpleNamespace(raw=b"", items=items)
        with patch.object(editor, "installed_attachment_offsets", return_value=set()):
            found = editor.deletion_records(document, ["metal scraps", "Glass Scrap"])
        self.assertEqual(
            ["Metal Scraps"], [item.asset for item in found])

    def test_gui_repair_deletes_configured_items_after_saving(self) -> None:
        events: list[str] = []
        count_entry = SimpleNamespace(offset=10, value=2500)
        item = SimpleNamespace(
            entries={"ItemCount": count_entry}, count=2500,
            location=editor.LOC_BACKPACK, asset="Metal Scraps")
        app = SimpleNamespace(
            document=SimpleNamespace(items=[item], resources=[], entries=[], anchors=[]),
            pending={},
            status_var=SimpleNamespace(set=lambda _value: None),
            repair_combine_var=SimpleNamespace(get=lambda: True),
            repair_targets={},
            repair_asset_targets={},
            repair_delete_items=["Metal Scraps"],
            confirm_repair_var=SimpleNamespace(get=lambda: True),
            refill_storage_var=SimpleNamespace(get=lambda: False),
            refill_rocket_var=SimpleNamespace(get=lambda: True),
            _selected_items=lambda: [item],
            _sync_document=lambda: True,
            _refresh_values=lambda: events.append("refresh"),
            _save=lambda *, confirm: events.append(f"save:{confirm}") or True,
            _remove_configured_items=lambda: events.append("delete"),
            _remove_duplicate_stacks=lambda *, confirm, include_memory=False:
                events.append("combine"),
        )
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(count_entry, 0)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual(["refresh", "save:False", "delete", "combine"], events)

    def test_gui_configured_removal_purges_and_skips_when_none(self) -> None:
        calls: list[str] = []
        app = SimpleNamespace(
            document=SimpleNamespace(items=[]),
            repair_delete_items=["Iron"],
            confirm_delete_var=SimpleNamespace(get=lambda: False),
            keep_var=SimpleNamespace(get=lambda: 5),
            status_var=SimpleNamespace(set=lambda _value: None),
            _structural_guard=lambda: calls.append("guard") or True,
            _purge=lambda action: calls.append("purge"),
        )
        with patch.object(gui.save_io, "deletion_records", return_value=[]):
            gui.LastCaretakerEditor._remove_configured_items(app)
        self.assertEqual([], calls)
        with patch.object(gui.save_io, "deletion_records", return_value=["x"]):
            gui.LastCaretakerEditor._remove_configured_items(app)
        self.assertEqual(["guard", "purge"], calls)

    def test_delete_warning_can_be_disabled_or_declined(self) -> None:
        app, items, _statuses = DeleteShortcutGuiTests._app((4,))
        app.confirm_delete_var = SimpleNamespace(get=lambda: False)
        with patch.object(gui.messagebox, "askyesno") as prompt:
            gui.LastCaretakerEditor._delete_items(app)
        prompt.assert_not_called()
        self.assertEqual({items[0].entries["ItemCount"].offset: 0}, app.pending)

        calls: list[str] = []
        configured = SimpleNamespace(
            document=SimpleNamespace(), repair_delete_items=["Iron"],
            confirm_delete_var=SimpleNamespace(get=lambda: True),
            keep_var=SimpleNamespace(get=lambda: 5),
            status_var=SimpleNamespace(set=lambda _value: None),
            _structural_guard=lambda: True,
            _purge=lambda action: calls.append("purge"),
        )
        with (
            patch.object(gui.save_io, "deletion_records", return_value=["x"]),
            patch.object(gui.messagebox, "askyesno", return_value=False) as prompt,
        ):
            gui.LastCaretakerEditor._remove_configured_items(configured)
        prompt.assert_called_once()
        self.assertEqual([], calls)

    def test_deletion_records_protect_attachments_and_deduplicate_mirrors(self) -> None:
        items = [
            self._item("Iron", editor.LOC_BACKPACK, 0, 10),
            self._item("Iron", editor.LOC_BACKPACK, 0, 10),
            self._item("Weapon Attachment Laser", editor.LOC_BACKPACK, 1, 20),
            self._item("Iron", editor.LOC_BACKPACK, 1, 30),
        ]
        document = SimpleNamespace(raw=b"", items=items)
        with patch.object(editor, "installed_attachment_offsets", return_value={30}):
            found = editor.deletion_records(document, ["Iron", "Weapon Attachment Laser"])
        self.assertEqual([items[0]], found)

    def test_deletion_list_normalization_and_persistence_failure(self) -> None:
        self.assertEqual(["Bio Waste", "Iron"], editor.normalize_deletion_items(
            [" Iron ", "iron", "Bio Waste", "", 3]))
        self.assertEqual([], editor.normalize_deletion_items("Iron"))
        app = SimpleNamespace(
            repair_delete_items=[], _write_settings=lambda: True,
            status_var=SimpleNamespace(set=lambda _value: None),
        )
        self.assertTrue(gui.LastCaretakerEditor._apply_deletion_items(app, ["Iron", "iron"]))
        self.assertEqual(["Iron"], app.repair_delete_items)
        app._write_settings = lambda: False
        with patch.object(gui.messagebox, "showerror") as error:
            self.assertFalse(gui.LastCaretakerEditor._apply_deletion_items(app, ["Bio Waste"]))
        error.assert_called_once()
        self.assertEqual(["Iron"], app.repair_delete_items)

    def test_initial_save_failure_never_deletes_configured_items(self) -> None:
        events = []
        app, _, entry = RepairAndTopOffGuiTests()._editor(events, save_succeeds=False)
        app.repair_delete_items = ["Iron"]
        app._remove_configured_items = lambda: events.append("delete")
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_dialog_filters_adds_removes_and_persists_without_deleting(self) -> None:
        saved = []
        with (
            patch.object(gui, "_load_settings", return_value={"repair_delete_items": ["Bio Waste"]}),
            patch.object(gui.LastCaretakerEditor, "_refresh_saves", lambda self: None),
            patch.object(gui.LastCaretakerEditor, "_load_startup_save", lambda self: None),
            patch.object(gui.LastCaretakerEditor, "_write_settings",
                         lambda self: saved.append(self._current_settings()) or True),
        ):
            try:
                app = gui.LastCaretakerEditor()
            except gui.tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
            try:
                app.document = SimpleNamespace(
                    path=Path("test.sav"), items=[
                        self._item("Iron", editor.LOC_BACKPACK, 5, 10),
                        self._item("Bio Waste", editor.LOC_BACKPACK, 5, 20),
                        self._item("Bio Flesh", "World", 5, 30),
                    ],
                )
                app._configure_deletion_items()
                dialog = next(child for child in app.winfo_children() if isinstance(child, gui.tk.Toplevel))
                frame = dialog.winfo_children()[0]
                entry = next(child for child in frame.winfo_children() if isinstance(child, gui.ttk.Entry))
                combo = next(child for child in frame.winfo_children() if isinstance(child, gui.ttk.Combobox))
                body = next(child for child in frame.winfo_children()
                            if isinstance(child, gui.ttk.Frame) and any(
                                isinstance(node, gui.tk.Listbox) for node in child.winfo_children()))
                marked = next(child for child in body.winfo_children() if isinstance(child, gui.tk.Listbox))
                entry.insert(0, "bio* -waste")
                self.assertEqual(("Bio Flesh",), tuple(combo.cget("values")))
                combo.set("Bio Flesh")
                combo.event_generate("<<ComboboxSelected>>")
                self.assertEqual(("Bio Flesh", "Bio Waste"), marked.get(0, gui.tk.END))
                self.assertEqual((), tuple(combo.cget("values")))
                buttons = frame.winfo_children()[-1].winfo_children()
                marked.selection_set(1)
                next(button for button in buttons if button.cget("text") == "Remove from List").invoke()
                self.assertEqual(("Bio Flesh",), marked.get(0, gui.tk.END))
                self.assertEqual(["Bio Waste"], app.repair_delete_items)
                entry.delete(0, gui.tk.END)
                self.assertEqual(("Bio Waste", "Iron"), tuple(combo.cget("values")))
                next(button for button in buttons if button.cget("text") == "Save").invoke()
                self.assertEqual(["Bio Flesh"], saved[-1]["repair_delete_items"])
                self.assertEqual([5, 5, 5], [item.count for item in app.document.items])
                app._configure_deletion_items()
                dialog = next(child for child in app.winfo_children() if isinstance(child, gui.tk.Toplevel))
                dialog.destroy()
                self.assertEqual(["Bio Flesh"], app.repair_delete_items)
            finally:
                app.destroy()


class SurvivalFormatTests(unittest.TestCase):
    @staticmethod
    def _document(*, limits: bool = False, debug_names: bool = False,
                  health_only: bool = False) -> editor.SaveDocument:
        rows = []
        attributes = ("Health",) if health_only else ("Health", "Level", "Temperature")
        for attribute in attributes:
            values = {"Value": 75.0}
            if limits:
                values.update(MinValue=0.0, MaxValue=200.0, DefaultValue=100.0)
            props = b"".join(
                editor._pack_tag(name, editor._pack_type("FloatProperty"), struct.pack("<f", value))
                for name, value in values.items()
            )
            if debug_names:
                props += editor._pack_tag(
                    "DebugName", editor._pack_type("StrProperty"), editor._pack_fstring(attribute))
            rows.append(
                editor._pack_fstring(f"EVoyageSurvivalAttributeType::{attribute}")
                + props + editor._pack_fstring("None")
            )
        raw = editor._pack_tag(
            "SurvivalAttributes",
            editor._pack_type("MapProperty", editor._pack_type("StrProperty"),
                              editor._pack_type("StructProperty")),
            struct.pack("<ii", 0, len(rows)) + b"".join(rows),
        )
        anchors = [editor.TextAnchor(0, "ActorName", "BP_FirstPersonCharacter_New_C1")]
        anchors.extend(editor.scan_text_anchors(raw))
        return editor.SaveDocument(
            Path("synthetic.sav"), raw, editor.scan_scalars(raw), anchors, [],
            editor.DEFAULT_CHUNK_SIZE, 0,
        )

    def test_value_only_survival_fields_use_map_keys_without_debug_names(self) -> None:
        document = self._document()
        fields = editor.player_survival_fields(document.raw)
        self.assertEqual({"Health", "Level", "Temperature"}, set(fields))
        self.assertTrue(all(set(row) == {"Value"} for row in fields.values()))
        stats = dict(editor.player_stat_entries(document))
        self.assertEqual({"Health - Current", "Level - Current", "Temperature - Current"}, set(stats))
        for entry in stats.values():
            self.assertEqual(75.0, entry.value)
            self.assertEqual(75.0, struct.unpack_from("<f", document.raw, entry.offset)[0])

    def test_value_only_survival_fields_cannot_be_locked(self) -> None:
        document = self._document()
        self.assertEqual({}, editor.player_lock_states(document))
        for key in editor.PLAYER_LOCKS:
            with self.subTest(key=key):
                with self.assertRaisesRegex(editor.SaveFormatError, "does not serialize"):
                    editor.player_lock_edits(document, key, True)

    def test_saved_limits_keep_legacy_stats_and_locks(self) -> None:
        for debug_names in (False, True):
            with self.subTest(debug_names=debug_names):
                document = self._document(limits=True, debug_names=debug_names)
                stats = dict(editor.player_stat_entries(document))
                self.assertEqual(200.0, stats["Health - Maximum"].value)
                self.assertEqual({"health": False, "energy": False}, editor.player_lock_states(document))
                for key, spec in editor.PLAYER_LOCKS.items():
                    edits = editor.player_lock_edits(document, key, True)
                    row = editor.player_survival_fields(document.raw)[spec["attribute"]]
                    updated = {name: entry.value for name, entry in row.items()}
                    updated.update({entry.name: value for entry, value in edits})
                    self.assertEqual(spec["locked"], updated)

    def test_health_only_npc_map_is_not_treated_as_player(self) -> None:
        document = self._document(health_only=True)
        with self.assertRaisesRegex(editor.SaveFormatError, "found 0"):
            editor.player_survival_fields(document.raw)

    def test_progress_shows_current_values_and_disables_unsupported_locks(self) -> None:
        document = self._document()
        app = SimpleNamespace(
            document=document,
            progress_tree=Mock(get_children=Mock(return_value=())),
            _display_value=lambda entry: str(entry.value),
            player_lock_vars={key: Mock() for key in editor.PLAYER_LOCKS},
            player_lock_buttons={key: Mock() for key in editor.PLAYER_LOCKS},
            charge_status_var=Mock(),
        )
        gui.LastCaretakerEditor._populate_progress(app)
        for key in editor.PLAYER_LOCKS:
            app.player_lock_vars[key].set.assert_called_once_with(False)
            app.player_lock_buttons[key].configure.assert_called_once_with(state=gui.tk.DISABLED)
        labels = [call.kwargs["values"][0] for call in app.progress_tree.insert.call_args_list]
        self.assertIn("Player Health - Current", labels)
        self.assertIn("Player Charge - Current", labels)
        self.assertIn("disabled", app.charge_status_var.set.call_args.args[0])


class SurvivalEnhancerTests(unittest.TestCase):
    @staticmethod
    def _entry(label: str, offset: int, value: int = 100):
        return editor.Entry(
            0, offset, "Points", "IntProperty", value,
            f"ActorName=BP_GlobalActor_PecoLockStates_C_1 | Category=Item | PrimaryAssetName=Enhancer {label}",
        )

    def _document(self):
        entries = [
            self._entry("Character HaulMastery", 10),
            self._entry("Character Battery", 20),
            self._entry("Character Vitality", 30),
            self._entry("Character Scrapper", 40),
            self._entry("Character Power Management", 50),
        ]
        return SimpleNamespace(entries=entries)

    def test_selects_only_health_and_energy_enhancers(self) -> None:
        labels = [label for label, _ in editor.survival_enhancer_entries(self._document())]
        self.assertEqual(["Character Battery", "Character Vitality", "Character Power Management"], labels)

    def _app(self, document, events: list[str]):
        return SimpleNamespace(
            document=document,
            pending={},
            status_var=SimpleNamespace(set=lambda value: events.append(f"status:{value}")),
            _refresh_values=lambda: events.append("refresh"),
        )

    def test_boost_stages_value_on_three_enhancers(self) -> None:
        events: list[str] = []
        app = self._app(self._document(), events)
        with patch.object(gui.simpledialog, "askstring", return_value="999999") as ask:
            gui.LastCaretakerEditor._boost_survival_enhancers(app)
        self.assertEqual("999999", ask.call_args.kwargs["initialvalue"])
        self.assertEqual({20: 999999, 30: 999999, 50: 999999}, app.pending)
        self.assertIn("refresh", events)

    def test_boost_cancel_and_invalid_leave_pending_untouched(self) -> None:
        events: list[str] = []
        app = self._app(self._document(), events)
        with patch.object(gui.simpledialog, "askstring", return_value=None):
            gui.LastCaretakerEditor._boost_survival_enhancers(app)
        with (
            patch.object(gui.simpledialog, "askstring", return_value="lots"),
            patch.object(gui.messagebox, "showerror") as error,
        ):
            gui.LastCaretakerEditor._boost_survival_enhancers(app)
        error.assert_called_once()
        self.assertEqual({}, app.pending)

    def test_boost_reports_missing_enhancers(self) -> None:
        app = self._app(SimpleNamespace(entries=[]), [])
        with (
            patch.object(gui.messagebox, "showinfo") as info,
            patch.object(gui.simpledialog, "askstring") as ask,
        ):
            gui.LastCaretakerEditor._boost_survival_enhancers(app)
        info.assert_called_once()
        ask.assert_not_called()


class PlayerLockGuiTests(unittest.TestCase):
    def _app(self, events: list[str], locked: bool):
        var = SimpleNamespace(value=locked)
        var.get = lambda: var.value
        var.set = lambda value: setattr(var, "value", value)
        app = SimpleNamespace(
            player_lock_vars={"health": var},
            document=SimpleNamespace(path=Path("x.sav")),
            keep_var=SimpleNamespace(get=lambda: 5),
            status_var=SimpleNamespace(set=lambda value: events.append(f"status:{value}")),
            _structural_guard=lambda: True,
            _load=lambda path, confirm_discard: events.append("reload"),
        )
        return app, var

    def test_toggle_off_writes_and_reloads(self) -> None:
        events: list[str] = []
        app, var = self._app(events, locked=False)
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "set_player_lock", return_value=(2, None)) as setter,
        ):
            gui.LastCaretakerEditor._toggle_player_lock(app, "health")
        self.assertEqual(("health", False), setter.call_args.args[1:])
        self.assertEqual("reload", events[0])

    def test_toggle_cancel_reverts_checkbox(self) -> None:
        events: list[str] = []
        app, var = self._app(events, locked=True)
        with (
            patch.object(gui.messagebox, "askyesno", return_value=False),
            patch.object(gui.save_io, "set_player_lock") as setter,
        ):
            gui.LastCaretakerEditor._toggle_player_lock(app, "health")
        setter.assert_not_called()
        self.assertFalse(var.value)


class StartupBackupGuiTests(unittest.TestCase):
    def _app(self, events: list[str], candidates: list[Path], *, loads: bool = True):
        app = SimpleNamespace(
            settings={"last_save": "older.sav"},
            candidates=candidates,
            auto_load_latest_var=SimpleNamespace(get=lambda: True),
            repair_on_open_var=SimpleNamespace(get=lambda: False),
            _load=lambda path, **_kwargs: events.append(f"load:{path}") or loads,
            _create_backup=lambda: events.append("backup"),
        )
        app._open_save = lambda path, **kwargs: gui.LastCaretakerEditor._open_save(app, path, **kwargs)
        return app

    def test_startup_loads_newest_save_then_backs_up_without_prompt(self) -> None:
        events: list[str] = []
        newest, older = Path("newest.sav"), Path("older.sav")
        app = self._app(events, [newest, older])
        with patch.object(gui.messagebox, "askyesno") as prompt:
            gui.LastCaretakerEditor._load_startup_save(app)
        prompt.assert_not_called()
        self.assertEqual([f"load:{newest}", "backup"], events)

    def test_no_startup_save_does_not_load_or_back_up(self) -> None:
        events: list[str] = []
        gui.LastCaretakerEditor._load_startup_save(self._app(events, []))
        self.assertEqual([], events)

    def test_failed_startup_load_does_not_back_up(self) -> None:
        events: list[str] = []
        candidate = Path("failed.sav")
        gui.LastCaretakerEditor._load_startup_save(self._app(events, [candidate], loads=False))
        self.assertEqual([f"load:{candidate}"], events)

    def test_disabled_auto_load_does_not_open_or_back_up(self) -> None:
        events: list[str] = []
        app = self._app(events, [Path("newest.sav")])
        app.auto_load_latest_var = SimpleNamespace(get=lambda: False)
        app.status_var = Mock()
        gui.LastCaretakerEditor._load_startup_save(app)
        self.assertEqual([], events)

    def test_startup_backup_precedes_automatic_repair(self) -> None:
        events: list[str] = []
        app = self._app(events, [Path("newest.sav")])
        app.repair_on_open_var = SimpleNamespace(get=lambda: True)
        app._repair_and_top_off = lambda **kwargs: events.append(f"repair:{kwargs['automatic']}")
        gui.LastCaretakerEditor._load_startup_save(app)
        self.assertEqual(["load:newest.sav", "backup", "repair:True"], events)

    def test_startup_opens_maximized_with_saved_preferences_and_carried_view(self) -> None:
        settings = {"repair_combine": False, "geometry": "800x600+0+0"}
        calls: list[str] = []
        with (
            patch.object(gui, "_load_settings", return_value=settings),
            patch.object(gui.LastCaretakerEditor, "_refresh_saves", lambda self: None),
            patch.object(gui.LastCaretakerEditor, "_load_startup_save",
                         lambda self: calls.append(self.state())),
        ):
            try:
                app = gui.LastCaretakerEditor()
            except gui.tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
            try:
                self.assertEqual(["zoomed"], calls)
                self.assertEqual("Carried (all)", app.location_filter.get())
                self.assertTrue(app.show_empty_items.get())
                self.assertTrue(app.purge_include_world.get())
                self.assertFalse(app.repair_combine_var.get())
                self.assertTrue(app.auto_load_latest_var.get())
                self.assertFalse(app.repair_on_open_var.get())
                self.assertTrue(app.confirm_purge_var.get())
                self.assertTrue(app.confirm_repair_var.get())
                toolbar = app.inventory_tab.winfo_children()[0]
                maintenance = toolbar.winfo_children()[3]
                buttons = maintenance.pack_slaves()[:3]
                self.assertEqual(
                    ["Repair & Top Off", "Remove Duplicate Stacks", "Purge Empty Slots"],
                    [button.cget("text") for button in buttons],
                )
                style = gui.ttk.Style(app)
                for button, expected_style, color in zip(
                    buttons, ("Repair.TButton", "Combine.TButton", "Purge.TButton"),
                    ("#25803b", "#666666", "#b52b2b"),
                ):
                    self.assertEqual(expected_style, button.cget("style"))
                    self.assertEqual(color, style.lookup(expected_style, "background"))
            finally:
                app.destroy()


class OpenSaveAutomationTests(unittest.TestCase):
    def test_repair_runs_only_after_successful_user_open_when_enabled(self):
        for enabled, loaded in ((False, True), (True, True), (True, False)):
            with self.subTest(enabled=enabled, loaded=loaded):
                events = []
                app = SimpleNamespace(
                    _load=lambda *a, **k: events.append("load") or loaded,
                    repair_on_open_var=SimpleNamespace(get=lambda: enabled),
                    _repair_and_top_off=lambda **k: events.append(f"repair:{k['automatic']}"),
                    _create_backup=Mock(),
                )
                self.assertEqual(loaded, gui.LastCaretakerEditor._open_save(app, Path("slot.sav")))
                self.assertEqual(
                    ["load", "repair:True"] if enabled and loaded else ["load"], events,
                )
                app._create_backup.assert_not_called()

    def test_manual_reload_runs_open_actions_after_discard_check(self):
        for accept in (False, True):
            with self.subTest(accept=accept):
                app = SimpleNamespace(
                    document=SimpleNamespace(path=Path("slot.sav")), pending={1: 2},
                    status_var=Mock(), _open_save=Mock(),
                )
                with patch.object(gui.messagebox, "askyesno", return_value=accept):
                    gui.LastCaretakerEditor._reload(app, prompt=True)
                if accept:
                    app._open_save.assert_called_once_with(Path("slot.sav"), confirm_discard=False)
                else:
                    app._open_save.assert_not_called()

    def test_automatic_repair_ignores_selection_and_repair_warning(self):
        events = []
        app, item, entry = RepairAndTopOffGuiTests()._editor(events)
        app._selected_items = Mock(side_effect=AssertionError("Must repair all carried items"))
        with (
            patch.object(gui, "_game_is_running", return_value=False),
            patch.object(gui.messagebox, "askyesno") as prompt,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]) as repair,
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app, automatic=True)
        prompt.assert_not_called()
        self.assertEqual([item], repair.call_args.args[1])
        self.assertEqual(["sync", "refresh", "save:False", "combine:False:True"], events)

    def test_automatic_repair_save_failure_stops_deletion_and_combine(self):
        events = []
        app, _, entry = RepairAndTopOffGuiTests()._editor(events, save_succeeds=False)
        app.repair_delete_items = ["Iron"]
        app._remove_configured_items = Mock()
        with (
            patch.object(gui, "_game_is_running", return_value=False),
            patch.object(gui.messagebox, "askyesno") as prompt,
            patch.object(gui.save_io, "repair_and_top_off", return_value=[(entry, 999)]),
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app, automatic=True)
        prompt.assert_not_called()
        app._remove_configured_items.assert_not_called()
        self.assertEqual(["sync", "refresh", "save:False"], events)

    def test_automatic_repair_does_not_queue_changes_while_game_is_running(self):
        events = []
        app, _, _ = RepairAndTopOffGuiTests()._editor(events)
        with (
            patch.object(gui, "_game_is_running", return_value=True),
            patch.object(gui.messagebox, "showerror") as error,
            patch.object(gui.save_io, "repair_and_top_off") as repair,
        ):
            gui.LastCaretakerEditor._repair_and_top_off(app, automatic=True)
        repair.assert_not_called()
        error.assert_called_once()
        self.assertEqual({}, app.pending)
        self.assertEqual([], events)


class StaleReloadTests(unittest.TestCase):
    def _app(self, events: list[str], *, pending: dict | None = None):
        return SimpleNamespace(
            document=SimpleNamespace(path=Path("save.sav"), raw=b"old"),
            pending=pending or {},
            status_var=SimpleNamespace(set=lambda value: events.append(f"status:{value}")),
            _load=lambda path, confirm_discard: events.append(f"load:{path}") or True,
        )

    def test_fresh_document_is_not_reloaded(self) -> None:
        events: list[str] = []
        app = self._app(events)
        with patch.object(gui.save_io, "save_is_stale", return_value=False):
            self.assertTrue(gui.LastCaretakerEditor._sync_document(app))
        self.assertEqual([], events)

    def test_stale_document_is_reloaded_before_editing(self) -> None:
        events: list[str] = []
        app = self._app(events)
        with patch.object(gui.save_io, "save_is_stale", return_value=True):
            self.assertTrue(gui.LastCaretakerEditor._sync_document(app))
        self.assertEqual("load:save.sav", events[0])

    def test_stale_document_with_pending_edits_asks_first(self) -> None:
        events: list[str] = []
        app = self._app(events, pending={1: 2})
        with (
            patch.object(gui.save_io, "save_is_stale", return_value=True),
            patch.object(gui.messagebox, "askyesno", return_value=False),
        ):
            self.assertFalse(gui.LastCaretakerEditor._sync_document(app))
        self.assertEqual([], events)

    def test_repair_after_external_write_still_reaches_the_target(self) -> None:
        saves = editor.discover_saves()
        if not saves:
            self.skipTest("No installed Last Caretaker save")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / saves[0].name
            path.write_bytes(saves[0].read_bytes())
            document = editor.load_save(path)
            item = next(
                item for item in document.items
                if item.asset.casefold().startswith("ammo")
                and item.location == editor.LOC_BACKPACK
            )
            # The game writes the save after the editor loaded it.
            editor.apply_edits(document, [(item.entries["ItemCount"], 2352)], make_backup=False)
            self.assertTrue(editor.save_is_stale(document))
            document = editor.load_save(path)
            self.assertFalse(editor.save_is_stale(document))
            owned = [i for i in document.items if editor.is_carried(i.location) and i.count > 0]
            editor.apply_edits(document, editor.repair_and_top_off(document, owned), make_backup=False)
            refreshed = next(
                i for i in editor.load_save(path).items
                if i.asset == item.asset and i.location == editor.LOC_BACKPACK
            )
            self.assertEqual(2500, refreshed.count)


class ItemMapCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        gvas.clear_item_map_cache()
        self.addCleanup(gvas.clear_item_map_cache)

    def _counting_walk(self, calls: list):
        def walk(raw, *, root=None, names=("Items",)):
            calls.append(tuple(names))
            return {name: [SimpleNamespace(name=name)] for name in names}
        return patch.object(gvas, "_walk_item_maps", side_effect=walk)

    def test_items_and_attachments_share_one_cached_walk(self) -> None:
        calls: list = []
        raw = b"save-bytes" * 4
        with self._counting_walk(calls):
            items = gvas.find_item_maps(raw)
            attachments = gvas.find_item_maps(raw, name="Attachments")
            again = gvas.find_item_maps(bytes(bytearray(raw)))  # equal content, new object
        self.assertEqual([gvas._SHARED_WALK], calls)
        self.assertEqual("Items", items[0].name)
        self.assertEqual("Attachments", attachments[0].name)
        self.assertIs(items[0], again[0])

    def test_mutable_buffers_are_never_cached(self) -> None:
        calls: list = []
        with self._counting_walk(calls):
            gvas.find_item_maps(bytearray(b"abc"))
            gvas.find_item_maps(bytearray(b"abc"))
        self.assertEqual(2, len(calls))

    def test_carry_reuses_walk_for_same_length_scalar_change_only(self) -> None:
        calls: list = []
        old, new, longer = b"aaaa1111", b"aaaa2222", b"aaaa22223"
        with self._counting_walk(calls):
            gvas.find_item_maps(old)
            self.assertEqual(2, gvas.carry_item_map_cache(old, new))
            self.assertEqual(0, gvas.carry_item_map_cache(old, longer))
            gvas.find_item_maps(new, name="Attachments")
            gvas.find_item_maps(longer)
        self.assertEqual(2, len(calls))

    def _apply(self, entry_type: str) -> list:
        carried: list = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.sav"
            path.write_bytes(b"x")
            raw = bytes(16)
            document = SimpleNamespace(path=path, raw=raw, mirrors=[])
            entry = editor.Entry(0, 4, "Value", entry_type, 0 if entry_type != "BoolProperty" else False)
            value = True if entry_type == "BoolProperty" else 7
            with (
                patch.object(editor, "decompress_container", return_value=(raw, 131072)),
                patch.object(editor, "_write_raw", return_value=None),
                patch.object(editor, "_encoded_value", return_value=b"\x07"),
                patch.object(gvas, "carry_item_map_cache",
                             side_effect=lambda old, new: carried.append((old, new)) or 0),
            ):
                editor.apply_edits(document, [(entry, value)], make_backup=False)
        return carried

    def test_scalar_edit_carries_cache_but_bool_edit_does_not(self) -> None:
        carried = self._apply("IntProperty")
        self.assertEqual(1, len(carried))
        self.assertEqual(7, carried[0][1][4])
        self.assertEqual([], self._apply("BoolProperty"))

    def test_cache_does_not_keep_old_save_buffers_alive(self) -> None:
        def walk(raw, *, root=None, names=("Items",)):
            return {name: [SimpleNamespace(name=name)] for name in names}

        first, second = b"a" * 64, b"b" * 64
        before = sys.getrefcount(first)
        with patch.object(gvas, "_walk_item_maps", new=walk):
            gvas.find_item_maps(first)
            gvas.find_item_maps(second)
        self.assertEqual(before, sys.getrefcount(first))

    def test_only_the_two_most_recent_contents_stay_cached(self) -> None:
        calls: list = []
        first, second, third = b"a" * 32, b"b" * 32, b"c" * 32
        with self._counting_walk(calls):
            for raw in (first, second, third):
                gvas.find_item_maps(raw)
            self.assertEqual(3, len(calls))
            gvas.find_item_maps(second)
            gvas.find_item_maps(third)
            self.assertEqual(3, len(calls))
            gvas.find_item_maps(first)
            self.assertEqual(4, len(calls))

    def test_a_buffer_is_hashed_once_however_often_it_is_looked_up(self) -> None:
        calls: list = []
        raw = b"save-bytes" * 4
        with self._counting_walk(calls), patch.object(
                gvas.hashlib, "blake2b", wraps=gvas.hashlib.blake2b) as hasher:
            gvas.find_item_maps(raw)
            gvas.find_item_maps(raw, name="Attachments")
            gvas.find_item_maps(raw, name="Other")
        self.assertEqual(1, hasher.call_count)


class StructuralPerformanceTests(unittest.TestCase):
    @staticmethod
    def _map(name: str, pairs: list[tuple[str, bytes]]) -> bytes:
        payload = struct.pack("<ii", 0, len(pairs)) + b"".join(
            editor._pack_fstring(key) + value for key, value in pairs)
        return editor._pack_tag(
            name, editor._pack_type("MapProperty", editor._pack_type("StrProperty"),
                                   editor._pack_type("StructProperty")), payload)

    @classmethod
    def _raw(cls, count: int = 20, *, indices=None) -> bytes:
        terminator = editor._pack_fstring("None")
        entries = []
        for index in range(count) if indices is None else indices:
            data = editor._pack_tag(
                "ItemCount", editor._pack_type("IntProperty"), struct.pack("<i", index))
            data += editor._pack_tag(
                "Health", editor._pack_type("FloatProperty"), struct.pack("<f", 100))
            props = editor._pack_tag(
                "AssetName", editor._pack_type("StrProperty"), editor._pack_fstring("Food Ration"))
            props += editor._pack_tag(
                "Data", editor._pack_type("StructProperty"), data + terminator)
            if index % 10 == 0:
                props += cls._map("Attachments", [
                    ("Laser", data + terminator),
                ])
            entries.append((str(index), props + terminator))
        blob = cls._map("Items", entries) + terminator
        wrapper = editor._pack_tag(
            "ByteData", editor._pack_type("ArrayProperty", editor._pack_type("ByteProperty")),
            struct.pack("<i", len(blob)) + blob)
        actors = cls._map("Actors", [("pawns", wrapper + terminator)])
        return struct.pack("<I", len(actors) + len(terminator)) + actors + terminator

    @staticmethod
    def _signature(maps):
        return [
            (m.tag, m.chain, m.owner, m.label, m.is_module,
             [(e.index, e.start, e.end, e.key, e.value_start, e.props) for e in m.entries])
            for m in maps
        ]

    def setUp(self) -> None:
        gvas.clear_item_map_cache()
        self.addCleanup(gvas.clear_item_map_cache)

    def test_shared_walk_matches_single_walks_for_nested_maps(self) -> None:
        raw = self._raw()
        shared = gvas._walk_item_maps(raw, root=4, names=gvas._SHARED_WALK)
        for name in gvas._SHARED_WALK:
            alone = gvas._walk_item_maps(raw, root=4, names=(name,))[name]
            self.assertTrue(alone)
            self.assertEqual(self._signature(alone), self._signature(shared[name]))

    def test_map_entry_tags_are_decoded_once(self) -> None:
        raw = self._raw()
        with patch.object(gvas, "read_tag", wraps=gvas.read_tag) as reader:
            maps = gvas._walk_item_maps(raw, root=4, names=gvas._SHARED_WALK)
        decoded_offsets = [call.args[1] for call in reader.call_args_list]
        for entry in maps["Items"][0].entries:
            self.assertEqual(1, decoded_offsets.count(entry.props[0].start))

    def test_struct_array_element_tags_are_decoded_once_and_purge_verifies(self) -> None:
        terminator = editor._pack_fstring("None")
        elements = b""
        for i in range(3):
            value = editor._pack_tag(
                "ItemCount", editor._pack_type("IntProperty"), struct.pack("<i", i))
            elements += self._map("Items", [(str(i), value + terminator)]) + terminator
        header = editor._pack_tag("Rows", editor._pack_type("StructProperty"), elements)
        array = editor._pack_tag(
            "Rows", editor._pack_type("ArrayProperty", editor._pack_type("StructProperty")),
            struct.pack("<i", 3) + header)
        raw = struct.pack("<I", len(array) + len(terminator)) + array + terminator
        with patch.object(gvas, "read_tag", wraps=gvas.read_tag) as reader:
            maps = gvas._walk_item_maps(raw, root=4, names=gvas._SHARED_WALK)["Items"]
        self.assertEqual(["0", "1", "2"], [m.entries[0].key for m in maps])
        decoded_offsets = [call.args[1] for call in reader.call_args_list]
        for item_map in maps:
            self.assertEqual(1, decoded_offsets.count(item_map.tag.start))
        with patch.object(gvas, "find_root", return_value=4):
            new_raw, removed, _report = gvas.purge_map_entries(
                raw, lambda m, e, v: e.key == "1")
            self.assertEqual(1, removed)
            self.assertEqual([1, 0, 1], [
                len(m.entries) for m in gvas.find_item_maps(new_raw)])

    def test_exploratory_walk_keeps_entries_before_malformed_entry(self) -> None:
        raw = self._raw()
        maps = gvas._walk_item_maps(raw, root=4, names=gvas._SHARED_WALK)
        broken = bytearray(raw)
        offset = maps["Items"][0].entries[1].value_start
        struct.pack_into("<i", broken, offset, 2**30)
        result = gvas._walk_item_maps(bytes(broken), root=4, names=gvas._SHARED_WALK)
        self.assertEqual(["0"], [e.key for e in result["Items"][0].entries])
        self.assertEqual(1, len(result["Attachments"]))

    def test_purge_many_cuts_preserves_survivors_and_nested_maps(self) -> None:
        raw = self._raw(100)
        with patch.object(gvas, "find_root", return_value=4):
            new_raw, removed, report = gvas.purge_map_entries(
                raw, lambda item_map, entry, values: int(entry.key) % 2 == 0)
            self.assertEqual(50, removed)
            self.assertEqual(50, report[0][1])
            self.assertEqual(self._raw(indices=range(1, 100, 2)), new_raw)
            self.assertEqual(len(new_raw) - 4, struct.unpack_from("<I", new_raw)[0])
            maps = gvas.find_item_maps(new_raw)
            self.assertEqual([str(i) for i in range(1, 100, 2)],
                             [e.key for e in maps[0].entries])
            for entry in maps[0].entries:
                self.assertEqual(int(entry.key), gvas.entry_values(new_raw, entry)["Data.ItemCount"])
            self.assertEqual([], gvas.find_item_maps(new_raw, name="Attachments"))
            # Parse each surviving ancestor strictly to verify size/count headers.
            for tag in maps[0].chain:
                if tag.type_name == "MapProperty":
                    gvas.parse_map(new_raw, tag)

    def test_nested_and_parent_cuts_are_spliced_only_once(self) -> None:
        raw = self._raw(10)
        with patch.object(gvas, "find_root", return_value=4):
            new_raw, removed, _report = gvas.purge_map_entries(
                raw, lambda item_map, entry, values: item_map.tag.name == "Attachments"
                or entry.key == "0", names=gvas._SHARED_WALK)
            self.assertEqual(2, removed)
            maps = gvas.find_item_maps(new_raw)
            self.assertEqual(9, len(maps[0].entries))
            self.assertEqual([], gvas.find_item_maps(new_raw, name="Attachments"))
            gvas.parse_map(new_raw, maps[0].tag)
            self.assertEqual(self._raw(indices=range(1, 10)), new_raw)

    def test_attachment_only_cuts_preserve_parent_entries(self) -> None:
        raw = self._raw()
        with patch.object(gvas, "find_root", return_value=4):
            new_raw, removed, _report = gvas.purge_map_entries(
                raw, lambda *_: True, name="Attachments")
            self.assertEqual(2, removed)
            maps = gvas.find_item_maps(new_raw)
            self.assertEqual(20, len(maps[0].entries))
            self.assertEqual([0, 0], [
                len(m.entries) for m in gvas.find_item_maps(new_raw, name="Attachments")])
            for tag in maps[0].chain:
                if tag.type_name == "MapProperty":
                    gvas.parse_map(new_raw, tag)

    def test_purge_all_entries_leaves_valid_empty_map(self) -> None:
        raw = self._raw()
        with patch.object(gvas, "find_root", return_value=4):
            new_raw, removed, _report = gvas.purge_map_entries(raw, lambda *_: True)
            self.assertEqual(20, removed)
            self.assertEqual(self._raw(0), new_raw)
            item_map = gvas.find_item_maps(new_raw)[0]
            self.assertEqual([], item_map.entries)
            gvas.parse_map(new_raw, item_map.tag)

    def test_noop_purge_does_not_reload_and_failures_are_reported(self) -> None:
        for fail in (False, True):
            events = []
            app = SimpleNamespace(
                document=SimpleNamespace(path=Path("test.sav")),
                status_var=SimpleNamespace(set=lambda value: events.append(value)),
                _load=lambda *_args, **_kwargs: events.append("reload"),
            )
            def action(_document):
                if fail:
                    raise ValueError("Invalid save structure")
                return 0, None, []
            with patch.object(gui.messagebox, "showerror") as error:
                gui.LastCaretakerEditor._purge(app, action)
            self.assertNotIn("reload", events)
            if fail:
                error.assert_called_once_with("Remove failed", "Invalid save structure")
            else:
                error.assert_not_called()
                self.assertEqual(["Nothing matched; the save was not modified"], events)

    def test_no_matches_leave_raw_unchanged(self) -> None:
        raw = self._raw()
        with patch.object(gvas, "find_root", return_value=4):
            self.assertEqual((raw, 0, []), gvas.purge_map_entries(
                raw, lambda *_: False))


class GvasRootTests(unittest.TestCase):
    """``find_root`` must not accept a shifted read that merely looks like a tag."""

    HEADER = (
        b"GVAS" + struct.pack("<iiiHHHI", 3, 0, 0, 5, 4, 0, 0)
        + editor._pack_fstring("test") + struct.pack("<ii", 3, 0)
        + editor._pack_fstring("/Script/Voyage.TestSave")
    )
    END = editor._pack_fstring("None")

    @classmethod
    def _save(cls, props: bytes) -> tuple[bytes, int]:
        body = cls.HEADER + props
        return struct.pack("<I", len(body)) + body, 4 + len(cls.HEADER)

    @staticmethod
    def _version_tag() -> bytes:
        return editor._pack_tag("Version", editor._pack_type("IntProperty"), struct.pack("<i", 1))

    def test_root_directly_after_the_header(self) -> None:
        raw, start = self._save(self._version_tag() + self.END)
        self.assertEqual(start, gvas.find_root(raw))

    def test_stray_byte_before_the_root_is_skipped(self) -> None:
        raw, start = self._save(b"\x00" + self._version_tag() + self.END)
        self.assertEqual(start + 1, gvas.find_root(raw))

    def test_lookalike_tag_before_the_real_root_is_rejected(self) -> None:
        # The stray byte turns the name length 9 into the int32 0x900. A lenient
        # reader then decodes a 2304-byte "name" and resumes at ``target``, where
        # the string value below hides a well-formed type node and size.
        head = editor._pack_fstring("SlotName") + editor._pack_type("StrProperty")
        target = 4 + 0x900
        mimic = (
            editor._pack_type("IntProperty") + struct.pack("<iB", 4, 0) + struct.pack("<i", 7)
        )
        chars_start = 1 + len(head) + 5 + 4
        chars = b"x" * (target - chars_start) + mimic + b"\x00"
        payload = struct.pack("<i", len(chars)) + chars
        tag = head + struct.pack("<iB", len(payload), 0) + payload
        raw, start = self._save(b"\x00" + tag + self.END)

        lookalike, _ = gvas.read_tag(raw, start)
        self.assertTrue(
            lookalike is not None and lookalike.name
            and lookalike.type_name.endswith("Property"),
            "the trap no longer fools a lenient reader",
        )
        self.assertEqual(start + 1, gvas.find_root(raw))

    def test_unterminated_root_falls_back_to_the_first_readable_tag(self) -> None:
        raw, start = self._save(self._version_tag())
        self.assertEqual(start, gvas.find_root(raw))

    def test_no_readable_tag_is_an_error(self) -> None:
        raw, _start = self._save(bytes(64))
        with self.assertRaises(gvas.GvasError):
            gvas.find_root(raw)


class AutoSaveLayoutTests(unittest.TestCase):
    """The game's AutoSave files the player's inventory under a world cell.

    The manual slot keeps the pawn under ``pawns``. The AutoSave nests it in
    ``GameProgress.PersistentActors[<cell>].actors`` instead, so the owner key
    says nothing and the actor name is the player's identity.
    """

    PLAYER = "BP_FirstPersonCharacter_New_C1"
    CONTAINER = "BP_Inventory_Container_C_1"
    END = editor._pack_fstring("None")
    BACKPACK = [("Food Ration", 5), ("Metal Scraps", 0), ("Binoculars", 1)]

    def setUp(self) -> None:
        gvas.clear_item_map_cache()
        self.addCleanup(gvas.clear_item_map_cache)

    @classmethod
    def _component(cls, rows, *, label=None, module=False) -> bytes:
        entries = []
        for index, (asset, count) in enumerate(rows):
            data = (
                editor._pack_tag("ItemCount", editor._pack_type("IntProperty"),
                                 struct.pack("<i", count))
                + editor._pack_tag("Health", editor._pack_type("FloatProperty"),
                                   struct.pack("<f", 100))
            )
            props = (
                editor._pack_tag("AssetName", editor._pack_type("StrProperty"),
                                 editor._pack_fstring(asset))
                + editor._pack_tag("Data", editor._pack_type("StructProperty"), data + cls.END)
            )
            entries.append((str(index), props + cls.END))
        body = b""
        if label:
            text = (
                struct.pack("<iB", 0, 11) + editor._pack_fstring("ST_Voyage")
                + editor._pack_fstring(label)
            )
            body += editor._pack_tag("Name", editor._pack_type("TextProperty"), text)
        if module:
            body += editor._pack_tag(
                "ModuleState", editor._pack_type("IntProperty"), struct.pack("<i", 1))
        body += StructuralPerformanceTests._map("Items", entries) + cls.END
        return editor._pack_tag("Component", editor._pack_type("StructProperty"), body)

    @classmethod
    def _save(cls, cells) -> bytes:
        entries = []
        for cell, actors in cells:
            elements = b"".join(
                editor._pack_tag("ActorName", editor._pack_type("NameProperty"),
                                 editor._pack_fstring(actor))
                + b"".join(components) + cls.END
                for actor, components in actors
            )
            header = editor._pack_tag("actors", editor._pack_type("StructProperty"), elements)
            array = editor._pack_tag(
                "actors", editor._pack_type("ArrayProperty", editor._pack_type("StructProperty")),
                struct.pack("<i", len(actors)) + header)
            entries.append((cell, array + cls.END))
        persistent = StructuralPerformanceTests._map("PersistentActors", entries)
        progress = editor._pack_tag(
            "GameProgress", editor._pack_type("StructProperty"), persistent + cls.END)
        body = GvasRootTests.HEADER + progress + cls.END
        return struct.pack("<I", len(body)) + body

    def _scenario(self, backpack=None) -> bytes:
        backpack = self.BACKPACK if backpack is None else backpack
        crate = self._component([("Crate Bolts", 0), ("Crate Cloth", 3)], label="Backpack")
        return self._save([
            ("11:8", [(self.CONTAINER, [crate])]),
            ("119:43", [(self.PLAYER, [
                self._component(backpack, label="Backpack"),
                self._component([("Weapon BoltGun", 1)], label="Holster"),
                self._component([("HeadLight", 1)], label="Equipment"),
                self._component([("Recipe Torch", 0), ("Recipe Rope", 1)]),
                self._component(backpack, module=True),
            ])]),
        ])

    @staticmethod
    def _load(raw: bytes) -> editor.SaveDocument:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "AutoSave.sav"
            path.write_bytes(editor.compress_container(raw))
            return editor.load_save(path)

    def test_player_is_identified_by_actor_not_by_owner_key(self) -> None:
        maps = gvas.find_item_maps(self._scenario())
        self.assertEqual(
            [("11:8", self.CONTAINER)] + [("119:43", self.PLAYER)] * 5,
            [(item_map.owner, item_map.actor) for item_map in maps])
        self.assertEqual([False] + [True] * 5, [editor.is_player_map(m) for m in maps])

    def test_is_player_map_rules(self) -> None:
        for owner, actor, expected in (
            ("pawns", "", True),
            ("119:43", "BP_FirstPersonCharacter_New_C1", True),
            ("119:43", "BP_PlayerCharacter_C_0", True),
            ("11:8", "BP_Inventory_Container_C_1", False),
            ("11:8", "BP_PlayerBox_C_1", False),
            ("", "", False),
        ):
            with self.subTest(owner=owner, actor=actor):
                item_map = SimpleNamespace(owner=owner, actor=actor)
                self.assertEqual(expected, editor.is_player_map(item_map))

    def test_pawns_layout_is_still_recognised(self) -> None:
        item_map = gvas._walk_item_maps(StructuralPerformanceTests._raw(), root=4)["Items"][0]
        self.assertEqual(editor.PLAYER_OWNER, item_map.owner)
        self.assertTrue(editor.is_player_map(item_map))

    def test_components_are_classified_and_the_backpack_mirror_is_paired(self) -> None:
        raw = self._scenario()
        self.assertEqual(
            [editor.LOC_BACKPACK, editor.LOC_HOLSTER, editor.LOC_EQUIPPED,
             editor.LOC_CATALOG, editor.LOC_MODULE],
            [location for _start, _end, location in editor.component_ranges(raw)])
        self.assertEqual(2, len(editor.find_mirrors(raw)))

    def test_load_save_places_every_record_without_a_warning(self) -> None:
        document = self._load(self._scenario())
        self.assertEqual({
            editor.LOC_BACKPACK: 3, editor.LOC_HOLSTER: 1, editor.LOC_EQUIPPED: 1,
            editor.LOC_MODULE: 3, editor.LOC_CATALOG: 2, editor.LOC_CONTAINER: 2,
        }, dict(Counter(item.location for item in document.items)))
        self.assertEqual([], document.warnings)
        self.assertEqual(2, len(document.mirrors))
        self.assertEqual(1, editor.count_empty_slots(document))
        self.assertEqual(4, editor.count_empty_slots(document, player_only=False))

    def test_empty_slot_count_matches_what_purge_removes_when_attachments_are_empty(self) -> None:
        # The game leaves loose attachments at count 0; purging never deletes
        # them, so the "Remove N record(s)?" figure must not include them either.
        loose = ("DA_Weapon_Attachment_Laser", 0)
        raw = self._scenario(self.BACKPACK + [loose])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "AutoSave.sav"
            path.write_bytes(editor.compress_container(raw))
            document = editor.load_save(path)
            counted = editor.count_empty_slots(document)
            removed, _backup, _report = editor.purge_items(
                document, zero_count_only=True, make_backup=False)
            reloaded = editor.load_save(path)
        self.assertEqual(1, counted)
        self.assertEqual(counted, removed)
        self.assertEqual(0, editor.count_empty_slots(reloaded))
        assets = [
            gvas.entry_values(reloaded.raw, entry).get("AssetName")
            for item_map in gvas.find_item_maps(reloaded.raw)
            for entry in item_map.entries
        ]
        self.assertNotIn("Metal Scraps", assets)
        self.assertEqual(2, assets.count(loose[0]))  # backpack record + its module mirror

    def test_empty_slot_count_is_zero_when_only_protected_records_are_empty(self) -> None:
        raw = self._scenario([("Food Ration", 5), ("DA_Weapon_Attachment_Laser", 0)])
        document = self._load(raw)
        self.assertEqual(0, editor.count_empty_slots(document))

    def test_unlocated_components_produce_a_warning(self) -> None:
        with patch.object(editor, "component_ranges", return_value=[]):
            document = self._load(self._scenario())
        self.assertEqual(1, len(document.warnings))
        self.assertIn("were not found", document.warnings[0])

    def test_unreadable_structure_produces_a_warning_and_no_mirrors(self) -> None:
        with patch.object(gvas, "find_item_maps", side_effect=gvas.GvasError("boom")):
            document = self._load(self._scenario())
        self.assertEqual(1, len(document.warnings))
        self.assertIn("could not be read (boom)", document.warnings[0])
        self.assertEqual([], document.mirrors)

    def test_save_without_player_records_has_no_warning(self) -> None:
        self.assertEqual([], self._load(ResourceAmountTests()._raw()).warnings)


class LoadProgressTests(unittest.TestCase):
    """``load_save`` reports which stage a long read has reached."""

    STAGES = [
        "Decompressing the save", "Scanning values", "Reading the item structure",
        "Reading humans", "Finishing",
    ]

    def setUp(self) -> None:
        gvas.clear_item_map_cache()
        self.addCleanup(gvas.clear_item_map_cache)

    @staticmethod
    def _load(**kwargs) -> editor.SaveDocument:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Slot.sav"
            path.write_bytes(editor.compress_container(ResourceAmountTests()._raw()))
            return editor.load_save(path, **kwargs)

    def test_stages_are_reported_in_order(self) -> None:
        stages: list[str] = []
        self._load(progress=stages.append)
        self.assertEqual(self.STAGES, stages)

    def test_progress_is_optional(self) -> None:
        self.assertIsInstance(self._load(), editor.SaveDocument)
        self.assertIsInstance(self._load(progress=None), editor.SaveDocument)

    def test_every_stage_is_reported_when_the_structure_cannot_be_read(self) -> None:
        stages: list[str] = []
        with patch.object(editor, "component_ranges", side_effect=gvas.GvasError("boom")):
            self._load(progress=stages.append)
        self.assertEqual(self.STAGES, stages)


class RunInBackgroundTests(unittest.TestCase):
    """Heavy calls run on a worker thread while the caller keeps servicing Tk."""

    @staticmethod
    def _app():
        shown: list[str] = []
        paints: list[int] = []
        app = SimpleNamespace(
            _busy_messages=["Loading x.sav..."],
            busy_var=SimpleNamespace(set=shown.append),
            update_idletasks=lambda: paints.append(1),
        )
        return app, shown, paints

    def test_the_call_runs_on_another_thread_and_its_result_is_returned(self) -> None:
        app, _shown, _paints = self._app()
        seen: dict[str, object] = {}

        def work(first, second=0):
            thread = threading.current_thread()
            seen.update(ident=thread.ident, name=thread.name, daemon=thread.daemon)
            return first + second

        self.assertEqual(7, gui._run_in_background(app, work, 3, second=4))
        self.assertNotEqual(threading.get_ident(), seen["ident"])
        self.assertEqual("editor-worker", seen["name"])
        self.assertTrue(seen["daemon"])

    def test_an_exception_is_raised_again_on_the_calling_thread(self) -> None:
        app, _shown, _paints = self._app()

        def work():
            raise ValueError("boom")

        with self.assertRaisesRegex(ValueError, "boom"):
            gui._run_in_background(app, work)

    def test_the_caller_keeps_servicing_the_ui_while_the_worker_runs(self) -> None:
        app, _shown, paints = self._app()
        gui._run_in_background(app, time.sleep, 0.25)
        self.assertGreaterEqual(len(paints), 3)

    def test_progress_is_only_passed_when_requested(self) -> None:
        app, _shown, _paints = self._app()
        received: dict[str, object] = {}

        def work(**kwargs):
            received.update(kwargs)

        gui._run_in_background(app, work)
        self.assertEqual({}, received)
        gui._run_in_background(app, work, report=True)
        self.assertEqual(["progress"], list(received))
        self.assertTrue(callable(received["progress"]))

    def test_the_reported_stage_reaches_the_footer(self) -> None:
        app, shown, _paints = self._app()

        def work(progress):
            progress("Scanning values")
            time.sleep(0.15)

        gui._run_in_background(app, work, report=True)
        self.assertIn("Loading x.sav - Scanning values", shown)

    def test_bare_and_mock_apps_are_accepted(self) -> None:
        for app in (SimpleNamespace(), Mock()):
            with self.subTest(app=type(app).__name__):
                self.assertEqual("ok", gui._run_in_background(app, lambda: "ok"))

    def test_a_gate_already_held_by_the_caller_is_left_in_place(self) -> None:
        app, _shown, _paints = self._app()
        gate = gui._InputGate(app)
        app._input_gate = gate
        gui._run_in_background(app, time.sleep, 0.05)
        self.assertIs(gate, app._input_gate)

    def test_only_real_tk_widgets_are_gated(self) -> None:
        self.assertIsNone(gui._hold_input(SimpleNamespace()))
        self.assertIsNone(gui._hold_input(Mock()))

    def test_a_window_that_cannot_be_made_busy_is_reported_as_such(self) -> None:
        def refuse(*_args):
            raise gui.tk.TclError("window can't be busy")

        self.assertFalse(gui._InputGate(SimpleNamespace(tk=SimpleNamespace(call=refuse))).hold())


class PumpUiTests(unittest.TestCase):
    """One service of the UI thread while a worker runs."""

    @staticmethod
    def _app(stack=("Loading x.sav...",)):
        shown: list[str] = []
        calls: list[str] = []
        app = SimpleNamespace(
            _busy_messages=list(stack),
            busy_var=SimpleNamespace(set=shown.append),
            update_idletasks=lambda: calls.append("idle"),
            update=lambda: calls.append("update"),
        )
        return app, shown, calls

    def test_the_footer_shows_the_operation_then_its_stage(self) -> None:
        app, shown, _calls = self._app()
        stage = gui._Stage()
        with patch.object(gui.time, "monotonic", return_value=100.0):
            text = gui._pump_ui(app, stage, 100.0, "")
            stage.set("Scanning values")
            text = gui._pump_ui(app, stage, 100.0, text)
        self.assertEqual(["Loading x.sav", "Loading x.sav - Scanning values"], shown)
        self.assertEqual("Loading x.sav - Scanning values", text)

    def test_elapsed_seconds_appear_after_the_first_second(self) -> None:
        app, shown, _calls = self._app()
        stage = gui._Stage()
        stage.set("Scanning values")
        with patch.object(gui.time, "monotonic", return_value=165.0):
            gui._pump_ui(app, stage, 100.0, "")
        self.assertEqual(["Loading x.sav - Scanning values (65 s)"], shown)
        shown.clear()
        with patch.object(gui.time, "monotonic", return_value=100.9):
            gui._pump_ui(app, gui._Stage(), 100.0, "")
        self.assertEqual(["Loading x.sav"], shown)

    def test_unchanged_text_is_not_written_again(self) -> None:
        app, shown, _calls = self._app()
        stage = gui._Stage()
        with patch.object(gui.time, "monotonic", return_value=100.0):
            text = ""
            for _ in range(3):
                text = gui._pump_ui(app, stage, 100.0, text)
        self.assertEqual(["Loading x.sav"], shown)

    def test_the_innermost_operation_is_the_one_shown(self) -> None:
        app, shown, _calls = self._app(("Repair & Top Off...", "Saving..."))
        with patch.object(gui.time, "monotonic", return_value=100.0):
            gui._pump_ui(app, gui._Stage(), 100.0, "")
        self.assertEqual(["Saving"], shown)

    def test_the_pumping_flag_is_set_only_while_events_are_dispatched(self) -> None:
        app, _shown, _calls = self._app()
        seen: list[object] = []
        app.update_idletasks = lambda: seen.append(app._pumping)
        gui._pump_ui(app, gui._Stage(), time.monotonic(), "")
        self.assertEqual([True], seen)
        self.assertIs(False, app._pumping)

    def test_real_events_are_dispatched_only_behind_an_input_gate(self) -> None:
        app, _shown, calls = self._app()
        gui._pump_ui(app, gui._Stage(), time.monotonic(), "")
        app._input_gate = gui._InputGate(app)
        gui._pump_ui(app, gui._Stage(), time.monotonic(), "")
        self.assertEqual(["idle", "update"], calls)

    def test_a_tcl_error_while_pumping_is_swallowed(self) -> None:
        app, _shown, _calls = self._app()

        def destroyed():
            raise gui.tk.TclError("application has been destroyed")

        app.update_idletasks = destroyed
        gui._pump_ui(app, gui._Stage(), time.monotonic(), "")
        self.assertIs(False, app._pumping)

    def test_the_pumping_flag_is_cleared_even_when_the_pump_fails(self) -> None:
        app, _shown, _calls = self._app()

        def broken():
            raise RuntimeError("boom")

        app.update_idletasks = broken
        with self.assertRaisesRegex(RuntimeError, "boom"):
            gui._pump_ui(app, gui._Stage(), time.monotonic(), "")
        self.assertIs(False, app._pumping)


class NotWhilePumpingTests(unittest.TestCase):
    """A command fired by an event dispatched during a pump is ignored."""

    class Probe:
        def __init__(self, pumping=False):
            self._pumping = pumping
            self.calls = []

        @gui._not_while_pumping
        def command(self, value, *, flag=False):
            """Runs the command."""
            self.calls.append((value, flag))
            return "ran"

    def test_a_normal_call_runs_and_returns_its_value(self) -> None:
        probe = self.Probe()
        self.assertEqual("ran", probe.command(1, flag=True))
        self.assertEqual([(1, True)], probe.calls)

    def test_the_wrapped_method_keeps_its_identity(self) -> None:
        self.assertEqual("command", self.Probe.command.__name__)
        self.assertEqual("Runs the command.", self.Probe.command.__doc__)

    def test_a_call_fired_by_a_pumped_event_is_ignored(self) -> None:
        probe = self.Probe(pumping=True)
        self.assertIsNone(probe.command(1))
        self.assertEqual([], probe.calls)

    def test_only_a_real_true_flag_blocks_the_call(self) -> None:
        for value in (Mock(), "yes", 1, None):
            with self.subTest(flag=value):
                self.assertEqual("ran", self.Probe(pumping=value).command(0))

    def test_every_long_running_command_is_guarded(self) -> None:
        for name in (
            "_load", "_reload", "_repair_and_top_off", "_purge_empty_slots",
            "_remove_duplicate_stacks", "_mark_blueprints_scanned", "_purge", "_save",
            "_create_backup", "_restore_backup",
        ):
            with self.subTest(command=name):
                self.assertTrue(hasattr(getattr(gui.LastCaretakerEditor, name), "__wrapped__"))

    def test_a_player_lock_click_during_a_pump_is_undone(self) -> None:
        key = next(iter(editor.PLAYER_LOCKS))
        var = SimpleNamespace(get=lambda: True, set=Mock())
        app = SimpleNamespace(player_lock_vars={key: var}, _pumping=True, _structural_guard=Mock())
        gui.LastCaretakerEditor._toggle_player_lock(app, key)
        var.set.assert_called_once_with(False)
        app._structural_guard.assert_not_called()


class LoadWarningGuiTests(unittest.TestCase):
    """What ``_load`` shows, and when, around the background read."""

    PATH = Path("Slot_AutoSave_0.sav")
    PROBLEM = "the player's inventory components were not found"

    @staticmethod
    def _app():
        statuses: list[str] = []
        app = SimpleNamespace(
            pending={},
            document=None,
            save_var=SimpleNamespace(set=lambda _value: None),
            status_var=SimpleNamespace(set=statuses.append),
            title=Mock(),
            _capture_views=Mock(return_value={}),
            _restore_views=Mock(),
            _update_pending=Mock(),
            _populate_progress=Mock(),
            _populate_inventory=Mock(),
            _populate_scalars=Mock(),
            _populate_resources=Mock(),
            _populate_humans=Mock(),
            _reset_map=Mock(),
            _map_tab_changed=Mock(),
            _show_human_selection=Mock(),
            _populate_backups=Mock(),
        )
        return app, statuses

    @staticmethod
    def _document(warnings=None):
        document = SimpleNamespace(
            path=LoadWarningGuiTests.PATH, entries=[1, 2, 3], items=[1, 2],
            human_data=gui.human_io.HumanData(),
        )
        if warnings is not None:
            document.warnings = warnings
        return document

    def _load(self, app, document):
        with patch.object(gui.save_io, "load_save", return_value=document):
            return gui.LastCaretakerEditor._load(app, self.PATH, confirm_discard=False)

    def test_a_structure_warning_reaches_the_status_and_a_dialog(self) -> None:
        app, statuses = self._app()
        busy_at_dialog: list[bool] = []
        with patch.object(
            gui.messagebox, "showwarning",
            side_effect=lambda *_a, **_k: busy_at_dialog.append(gui._is_busy(app)),
        ) as warn:
            self.assertTrue(self._load(app, self._document([self.PROBLEM])))
        self.assertEqual(1, len(statuses))
        self.assertIn("WARNING: " + self.PROBLEM, statuses[0])
        warn.assert_called_once()
        title, message = warn.call_args.args
        self.assertEqual("Inventory structure", title)
        self.assertIn(f"{self.PATH.name} loaded, but {self.PROBLEM}", message)
        self.assertEqual([False], busy_at_dialog)

    def test_a_clean_load_shows_no_warning(self) -> None:
        for warnings in ([], None):
            with self.subTest(warnings=warnings):
                app, statuses = self._app()
                with patch.object(gui.messagebox, "showwarning") as warn:
                    self.assertTrue(self._load(app, self._document(warnings)))
                warn.assert_not_called()
                self.assertNotIn("WARNING", statuses[0])
                self.assertIn("Loaded Slot_AutoSave_0.sav: 3 editable scalars", statuses[0])

    def test_the_file_is_read_off_the_ui_thread_with_progress_reporting(self) -> None:
        app, _statuses = self._app()
        seen: dict[str, object] = {}

        def fake(path, progress=None):
            seen.update(ident=threading.get_ident(), path=path, progress=progress)
            return self._document([])

        with patch.object(gui.save_io, "load_save", new=fake):
            gui.LastCaretakerEditor._load(app, self.PATH, confirm_discard=False)
        self.assertNotEqual(threading.get_ident(), seen["ident"])
        self.assertEqual(self.PATH, seen["path"])
        self.assertTrue(callable(seen["progress"]))

    def test_a_failed_load_reports_the_error_once_the_busy_state_has_ended(self) -> None:
        app, statuses = self._app()
        previous = SimpleNamespace(path=Path("Other.sav"))
        app.document = previous
        busy_at_dialog: list[bool] = []
        with (
            patch.object(gui.save_io, "load_save", side_effect=ValueError("bad container")),
            patch.object(
                gui.messagebox, "showerror",
                side_effect=lambda *_a, **_k: busy_at_dialog.append(gui._is_busy(app)),
            ) as error,
        ):
            self.assertFalse(gui.LastCaretakerEditor._load(app, self.PATH, confirm_discard=False))
        error.assert_called_once_with("Could not open save", "bad container")
        self.assertEqual([False], busy_at_dialog)
        self.assertIs(previous, app.document)
        self.assertEqual([], statuses)
        app.title.assert_not_called()
        for populate in (
            app._populate_progress, app._populate_inventory, app._populate_scalars,
            app._populate_resources, app._populate_humans, app._populate_backups,
        ):
            populate.assert_not_called()


class WorkerThreadingTests(unittest.TestCase):
    """Commands run their heavy calls off the UI thread but read Tk variables on it."""

    KEEP = 7

    def setUp(self) -> None:
        self.main = threading.get_ident()
        self.threads: dict[str, int] = {}

    def _recorder(self, name: str, result=None):
        def call(*_args, **_kwargs):
            self.threads[name] = threading.get_ident()
            return result
        return call

    def _var(self, name: str, value):
        return SimpleNamespace(get=self._recorder(name, value), set=lambda _value: None)

    def _app(self, **extra):
        return SimpleNamespace(
            document=SimpleNamespace(path=Path("Slot.sav"), entries=[]),
            pending={},
            status_var=SimpleNamespace(set=lambda _text: None),
            keep_var=self._var("keep_var", self.KEEP),
            purge_include_world=self._var("include_world", False),
            confirm_purge_var=self._var("confirm_purge", False),
            confirm_save_var=self._var("confirm_save", False),
            confirm_delete_var=self._var("confirm_delete", False),
            _structural_guard=lambda: True,
            _purge=Mock(),
            _load=Mock(return_value=True),
            _populate_backups=Mock(),
            **extra,
        )

    def _assert_off_the_ui_thread(self, *names: str) -> None:
        for name in names:
            with self.subTest(call=name):
                self.assertIn(name, self.threads)
                self.assertNotEqual(self.main, self.threads[name])

    def _assert_on_the_ui_thread(self, *names: str) -> None:
        for name in names:
            with self.subTest(variable=name):
                self.assertEqual(self.main, self.threads.get(name))

    def _assert_action_reads_no_tk_variable(self, app) -> None:
        """The purge action runs on a worker, so it may only use values read beforehand."""
        action = app._purge.call_args.args[0]
        app.keep_var.get = Mock(side_effect=AssertionError("Tk variable read in the worker"))
        with (
            patch.object(gui.save_io, "purge_items", return_value=(1, None, None)) as purge,
            patch.object(gui.save_io, "deletion_records", return_value=[]),
        ):
            action(app.document)
        self.assertEqual(self.KEEP, purge.call_args.kwargs["max_backups"])

    def test_purging_empty_slots_scans_off_thread(self) -> None:
        app = self._app()
        with patch.object(gui.save_io, "count_empty_slots", self._recorder("scan", 2)):
            gui.LastCaretakerEditor._purge_empty_slots(app)
        self._assert_off_the_ui_thread("scan")
        self._assert_on_the_ui_thread("include_world", "keep_var")
        self._assert_action_reads_no_tk_variable(app)

    def test_duplicate_removal_scans_off_thread(self) -> None:
        app = self._app()
        duplicates = Mock(side_effect=self._recorder("duplicates", []))
        with (
            patch.object(
                gui.save_io, "find_duplicate_stacks", duplicates,
            ),
            patch.object(
                gui.save_io, "count_unpurgeable_duplicate_stacks",
                self._recorder("unpurgeable", 0)),
        ):
            gui.LastCaretakerEditor._remove_duplicate_stacks(app)
        self._assert_off_the_ui_thread("duplicates", "unpurgeable")
        self.assertFalse(duplicates.call_args.kwargs["include_memory"])

    def test_duplicate_removal_reads_the_backup_count_before_the_worker_starts(self) -> None:
        app = self._app()
        duplicates = [SimpleNamespace(asset="Food Ration")]
        with patch.object(gui.save_io, "find_duplicate_stacks", return_value=duplicates):
            gui.LastCaretakerEditor._remove_duplicate_stacks(app, confirm=False)
        self._assert_on_the_ui_thread("keep_var")
        self._assert_action_reads_no_tk_variable(app)

    def test_configured_item_deletion_scans_off_thread(self) -> None:
        app = self._app(repair_delete_items=["Junk"])
        with patch.object(gui.save_io, "deletion_records", self._recorder("records", [])):
            gui.LastCaretakerEditor._remove_configured_items(app)
        self._assert_off_the_ui_thread("records")

    def test_configured_item_deletion_reads_the_backup_count_before_the_worker_starts(self) -> None:
        app = self._app(repair_delete_items=["Junk"])
        with patch.object(gui.save_io, "deletion_records", return_value=[SimpleNamespace()]):
            gui.LastCaretakerEditor._remove_configured_items(app)
        self._assert_on_the_ui_thread("keep_var")
        self._assert_action_reads_no_tk_variable(app)

    def test_the_purge_itself_runs_off_thread_and_the_save_is_reloaded(self) -> None:
        app = self._app()

        def action(_document):
            self.threads["purge"] = threading.get_ident()
            return 2, None, None

        gui.LastCaretakerEditor._purge(app, action)
        self._assert_off_the_ui_thread("purge")
        app._load.assert_called_once_with(app.document.path, confirm_discard=False)

    def test_blueprint_scan_runs_off_thread(self) -> None:
        app = self._app()
        status = self._recorder("scan", [("Rope", True)])
        with patch.object(gui.save_io, "blueprint_scan_status", status):
            gui.LastCaretakerEditor._mark_blueprints_scanned(app)
        self._assert_off_the_ui_thread("scan")

    def test_marking_blueprints_scanned_writes_off_thread_with_the_backup_count_read_first(self) -> None:
        app = self._app()
        written: dict[str, object] = {}

        def write(_document, names, **kwargs):
            self.threads["write"] = threading.get_ident()
            written.update(names=list(names), **kwargs)
            return list(names), Path("Slot.bak")

        with (
            patch.object(gui.save_io, "blueprint_scan_status", return_value=[("Rope", False)]),
            patch.object(gui.save_io, "mark_blueprints_scanned", write),
            patch.object(gui.messagebox, "askyesno", return_value=True),
        ):
            gui.LastCaretakerEditor._mark_blueprints_scanned(app)
        self._assert_off_the_ui_thread("write")
        self._assert_on_the_ui_thread("keep_var")
        self.assertEqual(
            {"names": ["Rope"], "make_backup": True, "max_backups": self.KEEP}, written)
        app._load.assert_called_once_with(app.document.path, confirm_discard=False)

    def test_the_staleness_check_runs_off_thread(self) -> None:
        app = SimpleNamespace(document=SimpleNamespace(path=Path("Slot.sav")), pending={})
        with patch.object(gui.save_io, "save_is_stale", self._recorder("stale", False)):
            self.assertTrue(gui.LastCaretakerEditor._sync_document(app))
        self._assert_off_the_ui_thread("stale")

    def test_the_game_check_runs_off_thread(self) -> None:
        app = SimpleNamespace(
            document=SimpleNamespace(path=Path("Slot.sav")), pending={},
            _sync_document=lambda: True)
        with patch.object(gui, "_game_is_running", self._recorder("game", False)):
            self.assertTrue(gui.LastCaretakerEditor._structural_guard(app))
        self._assert_off_the_ui_thread("game")

    def test_saving_checks_the_game_and_writes_off_thread(self) -> None:
        app = self._app()
        app.pending = {100: 5}
        app.document.entries = [SimpleNamespace(offset=100)]
        with (
            patch.object(gui, "_game_is_running", self._recorder("game", False)),
            patch.object(gui.save_io, "save_changes", self._recorder("write", (1, 0, None))),
        ):
            self.assertTrue(gui.LastCaretakerEditor._save(app, confirm=False))
        self._assert_off_the_ui_thread("game", "write")
        self._assert_on_the_ui_thread("keep_var")
        app._load.assert_called_once_with(app.document.path, confirm_discard=False)

    def test_creating_a_backup_copies_off_thread(self) -> None:
        app = self._app()
        with patch.object(gui.save_io, "backup_save", self._recorder("copy", Path("Slot.bak"))):
            gui.LastCaretakerEditor._create_backup(app)
        self._assert_off_the_ui_thread("copy")
        self._assert_on_the_ui_thread("keep_var")
        app._populate_backups.assert_called_once_with()

    def test_restoring_a_backup_checks_the_game_and_copies_off_thread(self) -> None:
        backups = SimpleNamespace(selection=lambda: ("0",), set=lambda _iid, _column: "Slot.bak")
        app = self._app(backup_tree=backups)
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui, "_game_is_running", self._recorder("game", False)),
            patch.object(gui.save_io, "restore_backup", self._recorder("restore")),
        ):
            gui.LastCaretakerEditor._restore_backup(app)
        self._assert_off_the_ui_thread("game", "restore")

    def test_a_player_lock_is_written_off_thread(self) -> None:
        key = next(iter(editor.PLAYER_LOCKS))
        var = SimpleNamespace(get=lambda: True, set=Mock())
        app = self._app(player_lock_vars={key: var})
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui.save_io, "set_player_lock", self._recorder("lock", (2, None))),
        ):
            gui.LastCaretakerEditor._toggle_player_lock(app, key)
        self._assert_off_the_ui_thread("lock")
        self._assert_on_the_ui_thread("keep_var")
        var.set.assert_not_called()


class ResponsiveGuiTests(unittest.TestCase):
    """The real window stays alive, yet closed to input, while a long operation runs."""

    @staticmethod
    def _make_app(startup=None):
        with (
            patch.object(gui, "_load_settings", return_value={}),
            patch.object(gui.LastCaretakerEditor, "_refresh_saves"),
            patch.object(
                gui.LastCaretakerEditor, "_load_startup_save",
                new=startup or (lambda _self: None)),
            patch.object(gui.LastCaretakerEditor, "_open_full_screen"),
        ):
            return gui.LastCaretakerEditor()

    def setUp(self) -> None:
        gvas.clear_item_map_cache()
        self.addCleanup(gvas.clear_item_map_cache)
        try:
            self.app = self._make_app()
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.app.withdraw()
        self.addCleanup(self._destroy, self.app)

    @staticmethod
    def _destroy(app) -> None:
        try:
            app.destroy()
        except gui.tk.TclError:
            pass  # a test may already have closed the window

    def test_save_controls_identify_local_saves_and_browsing(self) -> None:
        texts = []
        pending = list(self.app.winfo_children())
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, (gui.ttk.Label, gui.ttk.Button)):
                texts.append(widget.cget("text"))
        self.assertIn("Save file on this PC:", texts)
        self.assertIn("Browse...", texts)

    def test_options_are_in_settings_except_map_layers(self) -> None:
        app = self.app
        self.assertIn("Settings", [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()])
        pending = list(app.winfo_children())
        checkboxes = []
        configuration = []
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, gui.ttk.Checkbutton):
                checkboxes.append(widget)
            if isinstance(widget, gui.ttk.Spinbox) or (
                isinstance(widget, gui.ttk.Button)
                and widget.cget("text") in ("Stack Targets...", "Delete Items...")
            ):
                configuration.append(widget)
        self.assertGreater(len(checkboxes), 12)
        self.assertEqual(3, len(configuration))
        layers = set(app.map_view.layer_checks.values())
        self.assertTrue(layers.issubset(checkboxes))
        for widget in layers:
            self.assertTrue(str(widget).startswith(str(app.map_tab) + "."), widget)
        for widget in [check for check in checkboxes if check not in layers] + configuration:
            self.assertTrue(str(widget).startswith(str(app.settings_tab) + "."), widget)

    def test_settings_changes_are_saved_immediately_and_loaded_next_launch(self) -> None:
        app = self.app
        with tempfile.TemporaryDirectory() as folder, patch.object(
            gui, "SETTINGS_PATH", Path(folder) / "settings.json",
        ):
            pending = list(app.settings_tab.winfo_children())
            checkboxes = {}
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if isinstance(widget, gui.ttk.Checkbutton):
                    checkboxes[widget.cget("text")] = widget
            checkboxes["Auto Load Latest Save"].invoke()
            checkboxes["Repair and Top Off On Open"].invoke()
            checkboxes["Combine duplicate carried stacks"].invoke()
            checkboxes["Top Off Rocket Resources"].invoke()
            app._apply_repair_targets({
                key: str(2500 if key == "rocket.methane" else value)
                for key, value in app.repair_targets.items()
            })
            checkboxes["Show zero-count catalog"].invoke()
            checkboxes["Include environment/world in Purge"].invoke()
            app.map_view.layer_checks["player"].invoke()
            saved = gui._load_settings()
            for key, expected in (
                ("auto_load_latest_save", False), ("repair_on_open", True),
                ("repair_combine", False), ("show_empty_items", False),
                ("refill_rocket_resources", False),
                ("purge_include_world", False),
            ):
                self.assertIs(saved[key], expected)
            self.assertFalse(saved["map_layers"]["player"])
            with (
                patch.object(gui.LastCaretakerEditor, "_refresh_saves"),
                patch.object(gui.LastCaretakerEditor, "_load_startup_save"),
                patch.object(gui.LastCaretakerEditor, "_open_full_screen"),
            ):
                restored = gui.LastCaretakerEditor()
            try:
                self.assertFalse(restored.auto_load_latest_var.get())
                self.assertTrue(restored.repair_on_open_var.get())
                self.assertFalse(restored.repair_combine_var.get())
                self.assertFalse(restored.refill_rocket_var.get())
                self.assertEqual(2500, restored.repair_targets["rocket.methane"])
                self.assertFalse(restored.show_empty_items.get())
                self.assertFalse(restored.purge_include_world.get())
                self.assertFalse(restored.map_view.layer_vars["player"].get())
            finally:
                restored.destroy()

    def test_settings_write_failure_and_invalid_count_are_reported(self) -> None:
        app = self.app
        with (
            patch.object(app, "_write_settings", return_value=False),
            patch.object(gui.messagebox, "showerror") as error,
        ):
            self.assertFalse(app._save_preferences())
            error.assert_called_once_with("Settings failed", "Could not save editor settings.")
        app.keep_var.set("invalid")
        with tempfile.TemporaryDirectory() as folder, patch.object(
            gui, "SETTINGS_PATH", Path(folder) / "settings.json",
        ), patch.object(gui.messagebox, "showerror") as error:
            self.assertFalse(app._save_preferences())
            self.assertIn("Settings > Backups", error.call_args.args[1])
        app.keep_var.set(20)

    def test_on_open_repair_writes_synthetic_save_once_without_recursive_repairs(self) -> None:
        app = self.app
        app.repair_on_open_var.set(True)
        app.repair_combine_var.set(False)
        app.refill_storage_var.set(False)
        with tempfile.TemporaryDirectory() as folder:
            path = self._synthetic_save(folder)
            before = path.read_bytes()
            with (
                patch.object(gui, "_game_is_running", return_value=False),
                patch.object(gui.messagebox, "askyesno") as prompt,
                patch.object(app, "_repair_and_top_off", wraps=app._repair_and_top_off) as repair,
                patch.object(gui.save_io, "save_changes", wraps=gui.save_io.save_changes) as save,
            ):
                self.assertTrue(app._open_save(path))
            prompt.assert_not_called()
            repair.assert_called_once_with(automatic=True)
            save.assert_called_once()
            self.assertNotEqual(before, path.read_bytes())
            self.assertEqual({}, app.pending)
            reloaded = editor.load_save(path)
            carried = [item for item in reloaded.items if editor.is_carried(item.location) and item.count > 0]
            self.assertTrue(carried)
            for item in carried:
                self.assertEqual(
                    editor.repair_stack_target(item.asset, item.category, True, app.repair_targets),
                    item.count,
                )

    def test_rocket_target_dialog_keeps_save_buttons_and_guidance_visible(self) -> None:
        app = self.app
        app.deiconify()
        app._configure_repair_targets()
        app.update()
        dialog = next(w for w in app.winfo_children() if isinstance(w, gui.tk.Toplevel))
        try:
            widgets = []
            pending = list(dialog.winfo_children())
            while pending:
                widget = pending.pop()
                widgets.append(widget)
                pending.extend(widget.winfo_children())
            guidance = next(w for w in widgets if isinstance(w, gui.ttk.Label)
                            and "Higher than default rocket" in str(w.cget("text")))
            self.assertLessEqual(guidance.winfo_reqheight(), guidance.winfo_height())
            buttons = [w for w in widgets if isinstance(w, gui.ttk.Button)]
            self.assertTrue(any(w.cget("text") == "Save" for w in buttons))
            for button in buttons:
                self.assertTrue(button.winfo_ismapped())
                self.assertGreaterEqual(button.winfo_rootx(), dialog.winfo_rootx())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                     dialog.winfo_rootx() + dialog.winfo_width())
        finally:
            dialog.destroy()

    def test_settings_controls_fit_default_and_minimum_window_sizes(self) -> None:
        app = self.app
        app.deiconify()
        app.notebook.select(app.settings_tab)
        for size in ("1360x820", "1040x680"):
            app.geometry(size)
            app.update()
            for offset in (0, 1):
                app.settings_canvas.yview_moveto(offset)
                app.update()
                pending = list(app.settings_content.winfo_children())
                while pending:
                    widget = pending.pop()
                    pending.extend(widget.winfo_children())
                    if isinstance(widget, (gui.ttk.Checkbutton, gui.ttk.Spinbox, gui.ttk.Button)):
                        self.assertGreaterEqual(widget.winfo_rootx(), app.winfo_rootx(), (size, widget))
                        self.assertLessEqual(
                            widget.winfo_rootx() + widget.winfo_width(),
                            app.winfo_rootx() + app.winfo_width(), (size, widget),
                        )
            self.assertAlmostEqual(1.0, app.settings_canvas.yview()[1])

    def test_on_open_combine_writes_structure_without_repeating_repair(self) -> None:
        app = self.app
        app.repair_on_open_var.set(True)
        app.refill_storage_var.set(False)
        raw = AutoSaveLayoutTests()._scenario(backpack=[("Food Ration", 5), ("Food Ration", 2)])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "duplicates.sav"
            path.write_bytes(editor.compress_container(raw))
            with (
                patch.object(gui, "_game_is_running", return_value=False),
                patch.object(gui.messagebox, "askyesno") as prompt,
                patch.object(app, "_repair_and_top_off", wraps=app._repair_and_top_off) as repair,
                patch.object(gui.save_io, "save_changes", wraps=gui.save_io.save_changes) as save,
                patch.object(gui.save_io, "purge_items", wraps=gui.save_io.purge_items) as purge,
            ):
                self.assertTrue(app._open_save(path))
            prompt.assert_not_called()
            repair.assert_called_once_with(automatic=True)
            save.assert_called_once()
            purge.assert_called_once()
            carried = [item for item in editor.load_save(path).items
                       if editor.is_carried(item.location) and item.asset == "Food Ration"]
            self.assertEqual(1, len(carried))
            self.assertEqual(editor.repair_stack_target("Food Ration", "", True), carried[0].count)

    def _blocked(self) -> bool:
        app = self.app
        return bool(app.tk.getboolean(app.tk.call("tk", "busy", "status", app)))

    def _take_focus(self):
        app = self.app
        entry = gui.tk.Entry(app)
        entry.place(x=0, y=0, width=60, height=20)  # packing would leave it unmapped
        app.deiconify()
        app.attributes("-alpha", 0.0)
        app.update()
        entry.focus_force()
        app.update()
        if app.focus_get() is not entry:
            self.skipTest("The window cannot take keyboard focus here")
        return entry

    @staticmethod
    def _synthetic_save(folder: str) -> Path:
        path = Path(folder) / "Slot_AutoSave_0.sav"
        path.write_bytes(editor.compress_container(AutoSaveLayoutTests()._scenario()))
        return path

    def _count_beats(self, on_beat=None):
        """Count 20 ms timer callbacks, which only fire if the UI thread services Tk."""
        app = self.app
        beats: list[float] = []
        timer: dict[str, object] = {"id": None, "on": True}

        def beat() -> None:
            beats.append(time.monotonic())
            if on_beat is not None:
                on_beat()
            if timer["on"]:
                timer["id"] = app.after(20, beat)

        def stop() -> list[float]:
            timer["on"] = False
            app.after_cancel(timer["id"])
            return [later - earlier for earlier, later in zip(beats, beats[1:])]

        timer["id"] = app.after(20, beat)
        return beats, stop

    def test_timers_keep_firing_while_a_worker_runs(self) -> None:
        app = self.app
        beats, stop = self._count_beats()
        with gui._busy_ui(app, "Working..."):
            gui._run_in_background(app, time.sleep, 0.6)
        gaps = stop()
        self.assertGreaterEqual(len(beats), 8)
        self.assertLess(max(gaps), 0.35)

    def test_the_window_stays_responsive_during_a_slow_load(self) -> None:
        app = self.app
        footers: list[str] = []
        with tempfile.TemporaryDirectory() as folder:
            path = self._synthetic_save(folder)
            real = editor.load_save(path)

            def slow(_path, progress=None):
                progress("Scanning values")
                time.sleep(0.6)
                return real

            beats, stop = self._count_beats(lambda: footers.append(app.busy_var.get()))
            with patch.object(gui.save_io, "load_save", new=slow):
                self.assertTrue(app._load(path, confirm_discard=False))
            gaps = stop()
        self.assertGreaterEqual(len(beats), 8)
        self.assertLess(max(gaps), 0.35)
        self.assertIn(f"Loading {path.name} - Scanning values", footers)

    def test_input_is_blocked_only_while_an_operation_runs(self) -> None:
        app = self.app
        self.assertFalse(self._blocked())
        with gui._busy_ui(app, "Working..."):
            self.assertTrue(self._blocked())
            self.assertIsInstance(app._input_gate, gui._InputGate)
            shield = app._input_gate.shield
            self.assertTrue(shield.winfo_exists())
            self.assertEqual("watch", str(app.cget("cursor")))
        self.assertFalse(self._blocked())
        self.assertIsNone(app._input_gate)
        self.assertFalse(shield.winfo_exists())
        self.assertEqual("", str(app.cget("cursor")))

    def test_nested_busy_blocks_share_one_gate(self) -> None:
        app = self.app
        with gui._busy_ui(app, "Outer..."):
            gate = app._input_gate
            with gui._busy_ui(app, "Inner..."):
                self.assertIs(gate, app._input_gate)
            self.assertIs(gate, app._input_gate)
            self.assertTrue(self._blocked())
        self.assertIsNone(app._input_gate)
        self.assertFalse(self._blocked())

    def test_a_background_call_outside_a_busy_block_gates_input_while_it_runs(self) -> None:
        app = self.app
        seen: dict[str, object] = {}

        def work():
            seen["gate"] = app._input_gate
            time.sleep(0.05)

        gui._run_in_background(app, work)
        self.assertIsInstance(seen["gate"], gui._InputGate)
        self.assertIsNone(app._input_gate)
        self.assertFalse(self._blocked())

    def test_the_gate_is_released_when_the_operation_fails(self) -> None:
        app = self.app

        def boom():
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with gui._busy_ui(app, "Working..."):
                gui._run_in_background(app, boom)
        self.assertFalse(self._blocked())
        self.assertIsNone(app._input_gate)
        self.assertEqual([], app._busy_messages)

    def test_the_input_gate_cleans_up_after_itself(self) -> None:
        gate = gui._InputGate(self.app)
        self.assertTrue(gate.hold())
        self.assertEqual("break", gui._InputGate.discard(object()))
        self.assertTrue(gate.shield.bind("<Key>"))
        shield = gate.shield
        gate.release()
        self.assertIsNone(gate.shield)
        self.assertFalse(shield.winfo_exists())
        self.assertFalse(self._blocked())

    def test_key_presses_are_discarded_while_an_operation_runs(self) -> None:
        app = self.app
        entry = self._take_focus()
        with gui._busy_ui(app, "Working..."):
            self.assertIs(app._input_gate.shield, app.focus_get())
            entry.event_generate("<KeyPress-a>")
            app.update()
        self.assertEqual("", entry.get())
        self.assertIs(entry, app.focus_get())
        entry.event_generate("<KeyPress-b>")
        app.update()
        self.assertEqual("b", entry.get())

    def test_a_command_queued_before_the_operation_does_not_run_during_it(self) -> None:
        app = self.app
        app.document = SimpleNamespace(path=Path("Slot.sav"))
        app._populate_backups = Mock()
        with patch.object(gui.save_io, "backup_save", return_value=Path("Slot.bak")) as backup:
            app.after(10, app._create_backup)
            with gui._busy_ui(app, "Working..."):
                gui._run_in_background(app, time.sleep, 0.3)
            backup.assert_not_called()
            app._create_backup()
            backup.assert_called_once()

    def test_a_load_requested_while_pumping_is_ignored(self) -> None:
        app = self.app
        app._pumping = True
        with patch.object(gui.save_io, "load_save") as load:
            self.assertIsNone(app._load(Path("Slot.sav"), confirm_discard=False))
        load.assert_not_called()

    def test_a_real_load_leaves_no_busy_state_behind(self) -> None:
        app = self.app
        with tempfile.TemporaryDirectory() as folder:
            path = self._synthetic_save(folder)
            with (
                patch.object(gui.messagebox, "showwarning") as warn,
                patch.object(gui.messagebox, "showerror") as error,
            ):
                self.assertTrue(app._load(path, confirm_discard=False))
        warn.assert_not_called()
        error.assert_not_called()
        self.assertEqual(path, app.document.path)
        self.assertEqual([], app._busy_messages)
        self.assertIsNone(app._input_gate)
        self.assertFalse(self._blocked())
        self.assertEqual("", str(app.cget("cursor")))
        self.assertEqual("", app.busy_var.get())
        self.assertIn(path.name, app.title())

    def test_the_close_handler_exists_before_the_startup_load_begins(self) -> None:
        registered: list[str] = []

        def startup(app) -> None:
            registered.append(app.protocol("WM_DELETE_WINDOW"))

        other = self._make_app(startup)
        self.addCleanup(self._destroy, other)
        self.assertEqual(1, len(registered))
        self.assertTrue(registered[0])

    def test_closing_while_busy_asks_before_abandoning_the_operation(self) -> None:
        app = self.app
        app._quit_now = Mock()
        with (
            gui._busy_ui(app, "Working..."),
            patch.object(gui.messagebox, "askyesno", return_value=False) as ask,
        ):
            app._on_close()
        app._quit_now.assert_not_called()
        ask.assert_called_once()
        self.assertEqual("Operation in progress", ask.call_args.args[0])
        self.assertEqual("no", ask.call_args.kwargs["default"])
        self.assertEqual("warning", ask.call_args.kwargs["icon"])

    def test_confirming_the_prompt_quits_at_once(self) -> None:
        app = self.app
        app._quit_now = Mock()
        with (
            gui._busy_ui(app, "Working..."),
            patch.object(gui.messagebox, "askyesno", return_value=True),
        ):
            app._on_close()
        app._quit_now.assert_called_once_with()

    def test_closing_again_while_the_prompt_is_open_does_not_stack_prompts(self) -> None:
        app = self.app
        app._quit_now = Mock()
        prompts: list[str] = []

        def ask(title, *_args, **_kwargs):
            prompts.append(title)
            app._on_close()  # a second close request arrives while the prompt is open
            return False

        with (
            gui._busy_ui(app, "Working..."),
            patch.object(gui.messagebox, "askyesno", side_effect=ask),
        ):
            app._on_close()
        self.assertEqual(["Operation in progress"], prompts)
        self.assertIs(False, app._quit_prompt_open)

    def test_quitting_saves_settings_and_exits_without_waiting_for_the_worker(self) -> None:
        app = self.app
        with (
            patch.object(app, "_write_settings") as write,
            patch.object(gui.os, "_exit") as leave,
        ):
            app._quit_now()
        write.assert_called_once_with()
        leave.assert_called_once_with(0)
        with self.assertRaises(gui.tk.TclError):
            app.winfo_exists()

    def test_an_idle_close_still_asks_before_discarding_edits(self) -> None:
        app = self.app
        app.pending[1] = 5
        with patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
            app._on_close()
        ask.assert_called_once()
        self.assertEqual("Discard edits?", ask.call_args.args[0])
        self.assertTrue(app.winfo_exists())
        with (
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(app, "_write_settings") as write,
        ):
            app._on_close()
        write.assert_called_once_with()
        with self.assertRaises(gui.tk.TclError):
            app.winfo_exists()

    def test_an_idle_close_without_edits_does_not_ask(self) -> None:
        app = self.app
        with (
            patch.object(gui.messagebox, "askyesno") as ask,
            patch.object(app, "_write_settings") as write,
        ):
            app._on_close()
        ask.assert_not_called()
        write.assert_called_once_with()
        with self.assertRaises(gui.tk.TclError):
            app.winfo_exists()


class StructureTests(unittest.TestCase):
    """Cover the structural parser used for permanent item removal."""

    def _document(self) -> editor.SaveDocument:
        saves = editor.discover_saves()
        if not saves:
            self.skipTest("No installed Last Caretaker save")
        return editor.load_save(saves[0])

    def test_shared_walk_matches_single_name_walks(self) -> None:
        raw = self._document().raw

        def signature(maps):
            return [
                (m.tag.start, m.tag.end, m.owner, m.label, m.is_module, len(m.entries),
                 [tag.start for tag in m.chain])
                for m in maps
            ]

        shared = gvas._walk_item_maps(raw, names=gvas._SHARED_WALK)
        for name in gvas._SHARED_WALK:
            alone = gvas._walk_item_maps(raw, names=(name,))[name]
            self.assertTrue(alone, name)
            self.assertEqual(signature(alone), signature(shared[name]), name)

    def test_player_lock_round_trip_on_copy(self) -> None:
        initial = self._document()
        supported = editor.player_lock_states(initial)
        if not supported:
            self.skipTest("This save does not serialize survival limits needed for locks")
        source = initial.path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / source.name
            path.write_bytes(source.read_bytes())
            document = editor.load_save(path)
            for key in supported:
                for locked in (False, True, False):
                    editor.set_player_lock(document, key, locked, make_backup=False)
                    document = editor.load_save(path)
                    self.assertEqual(locked, editor.player_lock_states(document)[key])
                    fields = editor.player_survival_fields(document.raw)[editor.PLAYER_LOCKS[key]["attribute"]]
                    wanted = editor.PLAYER_LOCKS[key]["locked" if locked else "normal"]
                    self.assertEqual(wanted, {name: fields[name].value for name in wanted})
            self.assertEqual(len(document.raw), len(editor.load_save(source).raw))

    def test_scan_entries_rebuild_byte_for_byte(self) -> None:
        document = self._document()
        raw = document.raw
        tag = gvas.find_path(raw, editor.find_scanned_map(raw))[-1]
        _num_remove, entries = gvas.parse_map(raw, tag)
        self.assertTrue(entries)
        for entry in entries:
            key = gvas._flatten(raw, entry.key, "", {})
            value = gvas.entry_values(raw, entry)
            if value["FirstScanTimeUtc"] != value["LastScanTimeUtc"]:
                continue
            rebuilt = editor.build_scan_entry(
                key["PrimaryAssetName"], value["FirstScanTimeUtc"],
                asset_type=key["PrimaryAssetType.Name"], scan_count=value["ScanCount"])
            self.assertEqual(raw[entry.start:entry.end], rebuilt)

    def test_append_rejects_entries_of_the_wrong_shape(self) -> None:
        raw = self._document().raw
        with self.assertRaises(gvas.GvasError):
            gvas.append_map_entries(raw, editor.find_scanned_map(raw), [b"\x05\x00\x00\x00None\x00"])
        with self.assertRaises(ValueError):
            editor.build_scan_entry("bad name\x00", 1)

    def test_mark_blueprints_scanned_round_trip(self) -> None:
        source = self._document()
        blueprints = editor.known_blueprint_assets(source.raw)
        self.assertNotIn("DA_Memory_Drawings_Blueprints", blueprints)
        self.assertFalse(any(name.startswith("DA_Quest") for name in blueprints))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            before = editor.scanned_assets(document.raw)
            expected = sorted((n for n in blueprints if n not in before), key=str.casefold)
            added, _backup = editor.mark_blueprints_scanned(
                document, when=1_800_000_000, make_backup=False)
            self.assertEqual(expected, added)
            reloaded = editor.load_save(target)
            after = editor.scanned_assets(reloaded.raw)
            self.assertEqual(len(before) + len(added), len(after))
            for name, values in before.items():
                self.assertEqual(values, after[name])
            for name in added:
                self.assertEqual(
                    {"FirstScanTimeUtc": 1_800_000_000, "LastScanTimeUtc": 1_800_000_000,
                     "ScanCount": 1}, after[name])
            self.assertTrue(all(scanned for _n, scanned in editor.blueprint_scan_status(reloaded)))
            self.assertEqual(
                [(i.asset, i.location, i.count) for i in document.items],
                [(i.asset, i.location, i.count) for i in reloaded.items])
            self.assertEqual(([], None), editor.mark_blueprints_scanned(reloaded, make_backup=False))

    def test_tree_walk_finds_player_inventory(self) -> None:
        document = self._document()
        maps = gvas.find_item_maps(document.raw)
        self.assertGreater(len(maps), 10)
        player = [item_map for item_map in maps if editor.is_player_map(item_map)]
        self.assertTrue(player, "no Items map owned by the player pawn")
        for item_map in maps:
            self.assertEqual("MapProperty", item_map.tag.type_name)
            self.assertTrue(item_map.chain)
        values = gvas.entry_values(document.raw, player[0].entries[0])
        self.assertIn("AssetName", values)
        self.assertIn("Data.ItemCount", values)
        offsets = gvas.entry_field_offsets(document.raw, player[0].entries[0])
        self.assertIn("Data.ItemCount", offsets)

    def test_find_path_matches_tree_walk(self) -> None:
        document = self._document()
        item_map = gvas.find_item_maps(document.raw)[0]
        chain = gvas.find_path(document.raw, item_map.start)
        self.assertEqual(
            [tag.size_offset for tag in item_map.chain],
            [tag.size_offset for tag in chain],
        )

    def test_permanent_removal_keeps_the_save_loadable(self) -> None:
        source = self._document()
        victim = next(
            (item for item in source.items
             if item.location == editor.LOC_BACKPACK
             and item.count > 0
             and "ItemCount" in item.entries
             # Weapons, attachments and records nested inside a holstered
             # weapon are protected from structural removal.
             and not item.asset.startswith("Weapon")),
            None,
        )
        if victim is None:
            self.skipTest("No removable carried record")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            match = next(
                item for item in document.items
                if "ItemCount" in item.entries
                and item.entries["ItemCount"].offset == victim.entries["ItemCount"].offset
            )
            removed, backup, report = editor.purge_items(document, [match])
            self.assertEqual(1, removed)
            self.assertIsNotNone(backup)
            self.assertTrue(backup.is_file())
            self.assertEqual([(match.location, 1)], report)

            reloaded = editor.load_save(target)
            self.assertLess(len(reloaded.raw), len(document.raw))
            editor.validate_raw_save(reloaded.raw)
            # The slot is gone rather than left behind with a zero count.
            self.assertFalse([
                item for item in reloaded.items
                if "ItemCount" in item.entries
                and item.entries["ItemCount"].offset == match.entries["ItemCount"].offset
                and item.asset == match.asset
            ])
            before = sum(len(m.entries) for m in gvas.find_item_maps(document.raw))
            after = sum(len(m.entries) for m in gvas.find_item_maps(reloaded.raw))
            # The backpack is mirrored, so one visible record costs two entries.
            self.assertIn(before - after, (1, 2))

    def test_backpack_edits_reach_the_mirrored_copy(self) -> None:
        """The pawn stores its backpack twice; both copies must stay in step."""
        source = self._document()
        if not source.mirrors:
            self.skipTest("This save does not mirror the backpack")
        victim = next(
            (item for item in source.items
             if item.location == editor.LOC_BACKPACK
             and "ItemCount" in item.entries
             and editor.mirror_offset(
                 source.mirrors, item.entries["ItemCount"].offset) is not None),
            None,
        )
        if victim is None:
            self.skipTest("No mirrored backpack record")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            offset = victim.entries["ItemCount"].offset
            twin = editor.mirror_offset(document.mirrors, offset)
            entry = next(e for e in document.entries if e.offset == offset)
            new_value = int(entry.value) + 7
            changed, _backup = editor.apply_edits(
                document, [(entry, new_value)], make_backup=False)
            self.assertEqual(1, changed)

            reloaded = editor.load_save(target)
            self.assertEqual(
                new_value,
                struct.unpack_from("<i", reloaded.raw, offset)[0],
            )
            self.assertEqual(
                new_value,
                struct.unpack_from("<i", reloaded.raw, twin)[0],
                "the mirrored backpack copy was not updated",
            )

    def test_purge_empty_slots_removes_zero_count_records(self) -> None:
        source = self._document()
        victims = [
            item for item in source.items
            if item.location == editor.LOC_BACKPACK
            and item.count > 0
            and "ItemCount" in item.entries
            and not item.asset.startswith("Weapon")
        ][:2]
        if len(victims) < 2:
            self.skipTest("Not enough carried records")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            offsets = {item.entries["ItemCount"].offset for item in victims}
            edits = [
                (entry, 0) for entry in document.entries if entry.offset in offsets
            ]
            editor.apply_edits(document, edits, make_backup=False)

            document = editor.load_save(target)
            empty = editor.count_empty_slots(document)
            self.assertGreaterEqual(empty, len(victims))
            removed, _backup, _report = editor.purge_items(
                document, zero_count_only=True, make_backup=False)
            self.assertEqual(empty, removed)

            reloaded = editor.load_save(target)
            self.assertEqual(0, editor.count_empty_slots(reloaded))
            gvas.find_item_maps(reloaded.raw)

    def test_purge_can_remove_zero_count_environment_records(self) -> None:
        """Zero-count world/environment records (e.g. dropped items left at 0
        after being emptied) are only removed when player_only is disabled."""
        source = self._document()
        victim = next(
            (item for item in source.items
             if item.location in (editor.LOC_ENVIRONMENT, editor.LOC_CONTAINER)
             and item.count > 0
             and "ItemCount" in item.entries),
            None,
        )
        if victim is None:
            self.skipTest("No non-carried record with a positive count")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            entry = next(
                e for e in document.entries
                if e.offset == victim.entries["ItemCount"].offset
            )
            editor.apply_edits(document, [(entry, 0)], make_backup=False)

            document = editor.load_save(target)
            # Default (player_only=True) must never touch it.
            player_only_removed, _backup, _report = editor.purge_items(
                document, zero_count_only=True, make_backup=False)
            self.assertEqual(0, player_only_removed)

            reloaded = editor.load_save(target)
            entry2 = next(
                e for e in reloaded.entries if e.offset == entry.offset
            )
            self.assertEqual(0, entry2.value)

            # With player_only disabled, the zero-count world record is removed.
            removed, _backup, report = editor.purge_items(
                reloaded, zero_count_only=True, player_only=False, make_backup=False)
            self.assertGreaterEqual(removed, 1)
            self.assertTrue(all(label.startswith("World") for label, _count in report)
                             or any(label.startswith("World") for label, _count in report))

            final = editor.load_save(target)
            editor.validate_raw_save(final.raw)
            self.assertFalse([
                item for item in final.items
                if "ItemCount" in item.entries
                and item.entries["ItemCount"].offset == entry.offset
                and item.asset == victim.asset
            ])

    def test_find_duplicate_stacks_only_reports_purgeable_records(self) -> None:
        """Every duplicate found must actually be removable: it must resolve
        to an ``ItemCount`` reachable through a top-level ``Items`` map, not
        one nested inside a weapon's own ``Attachments`` sub-map."""
        document = self._document()
        duplicates = editor.find_duplicate_stacks(document)
        purgeable = editor._purgeable_item_count_offsets(document.raw)
        if not duplicates:
            self.skipTest("This save currently has no removable duplicate stacks")
        for item in duplicates:
            self.assertIn(item.entries["ItemCount"].offset, purgeable)

    def test_count_unpurgeable_duplicate_stacks_reports_detected_but_unremovable_stacks(
        self,
    ) -> None:
        """Duplicates a save's own structure prevents auto-removing must
        still be countable, so the GUI can tell "none exist" apart from
        "some exist but can't be safely removed yet" instead of reporting a
        flat, misleading "no duplicate stacks found"."""
        document = self._document()
        purgeable = editor.find_duplicate_stacks(document)
        all_detected = editor._all_duplicate_stack_candidates(document)
        unpurgeable = editor.count_unpurgeable_duplicate_stacks(document)
        self.assertEqual(unpurgeable, len(all_detected) - len(purgeable))
        self.assertGreaterEqual(unpurgeable, 0)

    def test_purge_never_removes_attachments_or_weapons_holding_them(self) -> None:
        source = self._document()
        victims = [
            item for item in source.items
            if "ItemCount" in item.entries and (
                "attachment" in item.asset.casefold()
                or item.asset.casefold().startswith("weapon boltgun")
            )
        ]
        if not victims:
            self.skipTest("No weapons or attachments in the live save")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            before = gvas.find_item_maps(document.raw, name="Attachments")
            chosen = [
                item for item in document.items
                if "ItemCount" in item.entries and (
                    "attachment" in item.asset.casefold()
                    or item.asset.casefold().startswith("weapon boltgun")
                )
            ]
            editor.purge_items(document, chosen, player_only=False, make_backup=False)
            reloaded = editor.load_save(target)
            after = gvas.find_item_maps(reloaded.raw, name="Attachments")
            self.assertEqual(
                sum(len(m.entries) for m in before),
                sum(len(m.entries) for m in after),
            )
            self.assertEqual(
                sorted(i.asset for i in document.items if "attachment" in i.asset.casefold()),
                sorted(i.asset for i in reloaded.items if "attachment" in i.asset.casefold()),
            )

    def test_remove_duplicate_stacks_round_trip_leaves_one_stack_each(self) -> None:
        source = self._document()
        duplicates = editor.find_duplicate_stacks(source)
        if not duplicates:
            self.skipTest("No duplicate carried stacks in the live save")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.path.name
            target.write_bytes(source.path.read_bytes())
            document = editor.load_save(target)
            targets = editor.find_duplicate_stacks(document)
            removed, _backup, _report = editor.purge_items(
                document, targets, player_only=False, make_backup=False)
            self.assertGreaterEqual(removed, len(targets))

            reloaded = editor.load_save(target)
            editor.validate_raw_save(reloaded.raw)
            self.assertEqual([], editor.find_duplicate_stacks(reloaded))
            maps = gvas.find_item_maps(reloaded.raw)
            self.assertTrue(maps)
            for item_map in maps:
                declared = struct.unpack_from(
                    "<ii", reloaded.raw, item_map.tag.payload
                )[1]
                self.assertEqual(
                    declared,
                    len(item_map.entries),
                    f"{item_map.tag.name} map at {item_map.tag.start} "
                    "became only partially parseable after removal",
                )


class ResourceAmountTests(unittest.TestCase):
    def test_storage_refill_actor_rules(self):
        for actor in (
            "BP_Module_Battery_C-87904259", "BP_Module_Oil_Tank_C-87903055",
            "BP_Module_Petrol_Container_C-88003045", "BP_Module_Diesel_ExtraLarge_Container_C-87812345",
            "BP_SUModule_Petrol_C-87912345",
            "BP_Module_Battery_C-88112345",
            "BP_Module_PetrolGenerator_Portable_C-87912345",
            "BP_Module_Diesel_Engine_Cylinder_C-87912345",
        ):
            record = editor.ResourceAmount("Electricity", actor, "", "Other", 0,
                                          editor.Entry(0, 0, "Resources.Electricity", "DoubleProperty", 0))
            with self.subTest(actor=actor):
                self.assertTrue(editor.is_refillable_storage(record))
        for actor in (
            "BP_Module_Battery_C_UAID_123",
            "BP_NPC_Angel_C-88010977", "BP_NPC_RamShark_C-88001070", "BP_NPC_Shark_C-88040454",
            "BP_NonPcStorage_C-87912345",
        ):
            record = editor.ResourceAmount("Electricity", actor, "", "Other", 0,
                                          editor.Entry(0, 0, "Resources.Electricity", "DoubleProperty", 100))
            with self.subTest(actor=actor):
                self.assertFalse(editor.is_refillable_storage(record))

    def test_storage_refill_default_and_overfill(self):
        entry = editor.Entry(0, 100, "Resources.Petrol", "DoubleProperty", 30000)
        record = editor.ResourceAmount("Petrol", "BP_Module_Petrol_Container_C-88003045",
                                      "", "Other", 0, entry)
        doc = SimpleNamespace(resources=[record])
        self.assertEqual(50000, editor.default_repair_targets()["storage.tanks"])
        self.assertEqual([(entry, 50000)], editor.storage_refill_edits(doc, 50000))
        for target in (0, -1, True, float("nan"), 1.5):
            with self.subTest(target=target), self.assertRaises(ValueError):
                editor.storage_refill_edits(doc, target)

    def test_refill_all_amounts_but_preserve_protected_condition_fields(self):
        def record(index, actor, resource="Electricity", amount=0, location="Other"):
            return editor.ResourceAmount(
                resource, actor, "", location, index,
                editor.Entry(index, index * 10, f"Resources.{resource}", "DoubleProperty", amount),
                editor.Entry(index + 1, index * 10 + 1, "Health", "FloatProperty", 200),
                editor.Entry(index + 2, index * 10 + 2, "ExtraDamage", "FloatProperty", 0),
            )
        npc = record(1, "BP_NPC_Shark_C-88040454")
        generator = record(2, "BP_Module_DieselGenerator_C-87886569", "Diesel")
        player = record(3, "BP_FirstPersonCharacter_New_C1", amount=float("inf"), location=editor.LOC_BACKPACK)
        world = record(4, "BP_Module_Battery_C_UAID_123")
        finite_player = record(5, "BP_FirstPersonCharacter_New_C1", amount=100, location=editor.LOC_BACKPACK)
        generator.health = npc.health
        doc = SimpleNamespace(resources=[npc, generator, player, world, finite_player])
        edits = editor.storage_refill_edits(doc, 50000)
        expected = {
            npc.entry.offset: 50000, player.entry.offset: 50000,
            world.entry.offset: 50000,
            generator.entry.offset: 50000, generator.extra_damage.offset: 10000,
            finite_player.entry.offset: 50000, finite_player.health.offset: 10000,
            finite_player.extra_damage.offset: 10000,
        }
        self.assertEqual(expected, {entry.offset: value for entry, value in edits})
        self.assertEqual(len(expected), len(edits))
        self.assertFalse(editor.is_refillable_storage(npc))
        self.assertTrue(editor.math.isinf(player.entry.value))

    def test_refill_resources_from_all_scalars_without_container_rows(self):
        entries = [
            editor.Entry(0, 100, "Resources.Custom1", "DoubleProperty", 75000),
            editor.Entry(1, 200, "Resources.Oxygen", "FloatProperty", 0),
            editor.Entry(2, 300, "Health", "FloatProperty", 20),
        ]
        doc = SimpleNamespace(entries=entries, resources=[])
        self.assertEqual([(entries[0], 12345), (entries[1], 12345)],
                         editor.storage_refill_edits(doc, 12345))

    def test_health_is_associated_only_with_unambiguous_actor_fields(self):
        health = editor.Entry(0, 100, "Health", "FloatProperty", 199.584442)
        damage = editor.Entry(1, 120, "ExtraDamage", "FloatProperty", 0)
        first = editor.ResourceAmount("Petrol", "Tank", "", "Other", 50,
                                     editor.Entry(2, 60, "Resources.Petrol", "DoubleProperty", 100))
        second = editor.ResourceAmount("Electricity", "Battery", "", "Other", 250,
                                      editor.Entry(3, 260, "Resources.Electricity", "DoubleProperty", 100))
        anchors = [editor.TextAnchor(0, "ActorName", "Tank"), editor.TextAnchor(200, "ActorName", "Battery")]
        ambiguous = [editor.Entry(4, 300, "Health", "FloatProperty", 1),
                     editor.Entry(5, 350, "Health", "FloatProperty", 2)]
        editor.attach_resource_health([first, second], [health, damage] + ambiguous, anchors, [])
        self.assertIs(health, first.health)
        self.assertIs(damage, first.extra_damage)
        self.assertIsNone(second.health)

    def _map(self, values, *, value_type="DoubleProperty", enum="EModuleResourceType"):
        key = editor._pack_type(
            "EnumProperty", editor._pack_type(enum, editor._pack_type("/Script/Voyage")),
            editor._pack_type("ByteProperty"),
        )
        payload = struct.pack("<ii", 0, len(values))
        for name, value in values:
            payload += editor._pack_fstring(f"{enum}::{name}")
            payload += struct.pack("<d" if value_type == "DoubleProperty" else "<f", value)
        return editor._pack_tag(
            "Resources", editor._pack_type("MapProperty", key, editor._pack_type(value_type)),
            payload,
        )

    def _raw(self, actors=None):
        header = (
            b"GVAS" + struct.pack("<iiiHHHI", 3, 0, 0, 5, 4, 0, 0)
            + editor._pack_fstring("test") + struct.pack("<ii", 3, 0)
            + editor._pack_fstring("/Script/Voyage.TestSave")
        )
        props = b""
        for actor, values in (actors if actors is not None else (
            ("BP_Boat_Barge_C1031LevelInstanceBP_Module_Petrol_Container_C_0", [("Diesel", 2000.0)]),
            ("BP_Module_Battery_C-123", [("Electricity", 1960.3122993827958)]),
            ("BP_Module_Petrol_Container_C-456", [("Petrol", 253.6095386722867), ("Oil", 0.0)]),
        )):
            props += editor._pack_tag(
                "ActorName", editor._pack_type("StrProperty"), editor._pack_fstring(actor),
            )
            blob = self._map(values) + editor._pack_fstring("None")
            props += editor._pack_tag(
                "ByteData", editor._pack_type("ArrayProperty", editor._pack_type("ByteProperty")),
                struct.pack("<i", len(blob)) + blob,
            )
        props += editor._pack_tag("Health", editor._pack_type("FloatProperty"), struct.pack("<f", 100))
        body = header + props + editor._pack_fstring("None")
        return struct.pack("<I", len(body)) + body

    def test_map_values_have_exact_offsets_and_owner_not_inferred_resource(self):
        raw = self._raw()
        records = editor.scan_resource_amounts(raw, editor.scan_text_anchors(raw), [])
        self.assertEqual(["Diesel", "Electricity", "Petrol", "Oil"], [r.resource for r in records])
        self.assertIn("Petrol_Container", records[0].actor)
        self.assertEqual(2000, records[0].entry.value)
        self.assertEqual(0, records[-1].entry.value)
        for record in records:
            self.assertEqual(record.entry.value, struct.unpack_from("<d", raw, record.entry.offset)[0])
            self.assertIn(record.actor, record.entry.context)

    def test_float_values_empty_maps_and_unrelated_enum_maps(self):
        raw = (
            self._map([("Petrol", 12.5)], value_type="FloatProperty")
            + self._map([]) + self._map([("Petrol", 99)], enum="OtherEnum")
        )
        records = editor.scan_resource_amounts(raw, [], [])
        self.assertEqual(1, len(records))
        self.assertEqual("FloatProperty", records[0].entry.type)
        self.assertEqual(12.5, records[0].entry.value)
        self.assertEqual("", records[0].actor)

    def test_malformed_resource_map_is_an_explicit_error(self):
        raw = bytearray(self._map([("Diesel", 1.0)]))
        tag, _ = gvas.read_tag(raw, 0)
        struct.pack_into("<i", raw, tag.payload + 4, 2)
        with self.assertRaisesRegex(editor.SaveFormatError, "Invalid Resources map"):
            editor.scan_resource_amounts(bytes(raw), [], [])

    def test_inventory_association_does_not_leak_into_world_storage(self):
        first, second = self._map([("Electricity", 12.5)]), self._map([("Petrol", 200)])
        raw = first + second
        item = editor.InventoryItem(0, "Back Battery Large", "Equipment", editor.PLAYER_OWNER,
                                    "Player Backpack", 0, {})
        maps = [SimpleNamespace(entries=[SimpleNamespace(start=0, end=len(first))])]
        records = editor.scan_resource_amounts(raw, [], [item], maps)
        self.assertEqual(("Back Battery Large", "Player Backpack"),
                         (records[0].asset, records[0].location))
        self.assertEqual("", records[1].asset)

    def test_save_reload_changes_only_resource_payload_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "resources.sav"
            raw = self._raw()
            path.write_bytes(editor.compress_container(raw))
            document = editor.load_save(path)
            self.assertEqual(4, len(document.resources))
            self.assertEqual(len(document.entries), len({e.offset for e in document.entries}))
            values = [3000.0, 6000.0, 500.0, 0.0]
            changed, backup = editor.apply_edits(
                document, [(r.entry, v) for r, v in zip(document.resources, values)],
                make_backup=False,
            )
            self.assertEqual(3, changed)
            self.assertIsNone(backup)
            reloaded = editor.load_save(path)
            self.assertEqual(values, [r.entry.value for r in reloaded.resources])
            expected = bytearray(raw)
            for record, value in zip(document.resources, values):
                struct.pack_into("<d", expected, record.entry.offset, value)
            self.assertEqual(bytes(expected), reloaded.raw)
            self.assertEqual(100, next(e.value for e in reloaded.entries if e.name == "Health"))
            self.assertTrue(all(r.entry in reloaded.entries for r in reloaded.resources))

    def test_resource_mirrors_are_written_together(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "resources.sav"
            raw = self._raw()
            path.write_bytes(editor.compress_container(raw))
            document = editor.load_save(path)
            first, twin = document.resources[:2]
            document.mirrors = [
                (first.entry.offset, first.entry.offset + 8, twin.entry.offset - first.entry.offset),
            ]
            editor.apply_edits(document, [(first.entry, 123.5)], make_backup=False)
            reloaded = editor.load_save(path)
            self.assertEqual([123.5, 123.5], [r.entry.value for r in reloaded.resources[:2]])

    def test_resource_amounts_reject_negative_nan_and_infinity(self):
        entry = editor.Entry(0, 0, "Resources.Diesel", "DoubleProperty", 1.0)
        for value in ("-1", "nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite, non-negative"):
                editor.parse_value(entry, value)
        self.assertEqual(0, editor.parse_value(entry, "0"))


class ScalarFilterPerformanceGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        with (
            patch.object(gui, "_load_settings", return_value={}),
            patch.object(gui.LastCaretakerEditor, "_refresh_saves"),
            patch.object(gui.LastCaretakerEditor, "_load_startup_save"),
            patch.object(gui.LastCaretakerEditor, "_open_full_screen"),
        ):
            try:
                self.app = gui.LastCaretakerEditor()
            except gui.tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        self.app.withdraw()
        self.addCleanup(self.app.destroy)

    def test_inventory_type_colors_survive_filter_sort_and_pending_value_refresh(self):
        app = self.app
        items = [
            editor.InventoryItem(
                index, asset, "Item", "Actor", editor.LOC_BACKPACK, 0,
                {"ItemCount": editor.Entry(index, 100 + index * 8, "ItemCount", "IntProperty", 5)},
            )
            for index, asset in enumerate(("Ammo Rifle", "Memory Toy", "Unrecognized item"))
        ]
        app.document = SimpleNamespace(items=items, entries=[], path=Path("Slot.sav"))
        app._populate_inventory()
        tree = app.inventory_tree
        for index, category in enumerate(("ammo", "memory", "other")):
            self.assertEqual((f"inventory:{category}",), tree.item(str(index), "tags"))
            self.assertEqual(gui.INVENTORY_TYPE_COLORS[category],
                             str(tree.tag_configure(f"inventory:{category}", "background")))
        tree.selection_set("1")
        tree.focus("1")
        app._sort_tree(tree, "asset")
        original_order = tree.get_children()
        app.pending[items[1].entries["ItemCount"].offset] = 15
        app._refresh_values()
        self.assertEqual(original_order, tree.get_children())
        self.assertEqual(("1",), tree.selection())
        self.assertEqual("1", tree.focus())
        self.assertEqual("15", tree.set("1", "count"))
        self.assertEqual(("inventory:memory",), tree.item("1", "tags"))
        style = gui.ttk.Style(app)
        self.assertNotEqual(
            gui.INVENTORY_TYPE_COLORS["memory"],
            style.lookup("Treeview", "background", ("selected",)),
        )
        app.item_filter_applied = "Memory"
        app._populate_inventory()
        self.assertEqual(("1",), tree.get_children())
        self.assertEqual(("inventory:memory",), tree.item("1", "tags"))

    def test_clear_large_sorted_filter_restores_all_rows_without_individual_moves(self) -> None:
        app = self.app
        entries = [
            editor.Entry(
                i, 100 + i * 8, "Health", "FloatProperty", float(i),
                "ActorName=OnlyMatch" if i == 0 else f"ActorName=Battery_{i}",
            )
            for i in range(10000)
        ]
        app.document = SimpleNamespace(entries=entries)
        app.pending[entries[0].offset] = 20000
        app.scalar_filter.set("OnlyMatch")
        app._apply_scalar_filter()
        self.assertEqual(("0",), app.scalar_tree.get_children())
        app._sort_tree(app.scalar_tree, "value")

        with (
            patch.object(app.scalar_tree, "move", side_effect=AssertionError("Quadratic row moves")),
            patch.object(app.scalar_tree, "set_children", wraps=app.scalar_tree.set_children) as reorder,
        ):
            app._clear_scalar_filter()

        expected = ("0", *(str(i) for i in range(len(entries) - 1, 0, -1)))
        self.assertEqual(expected, app.scalar_tree.get_children())
        reorder.assert_called_once_with("", *expected)
        self.assertEqual("", app.scalar_filter.get())
        self.assertEqual("", app.scalar_filter_applied)
        self.assertEqual({str(entry.index): entry for entry in entries}, app.scalar_rows)
        self.assertEqual({entries[0].offset: 20000}, app.pending)
        self.assertEqual("20000", app.scalar_tree.set("0", "value"))
        app.scalar_tree.selection_set("9999")
        with patch.object(app, "_prompt_entry") as prompt:
            app._edit_scalar()
        prompt.assert_called_once_with(entries[9999])

    def test_clear_unsorted_filter_restores_document_order(self) -> None:
        app = self.app
        entries = [
            editor.Entry(2, 100, "Health", "FloatProperty", 200, "ActorName=Battery"),
            editor.Entry(1, 200, "Level", "IntProperty", 3, "ActorName=NPC"),
        ]
        app.document = SimpleNamespace(entries=entries)
        app.scalar_filter.set("he?lth -NPC")
        app._apply_scalar_filter()
        self.assertEqual(("2",), app.scalar_tree.get_children())
        with patch.object(app.scalar_tree, "set_children") as reorder:
            app._clear_scalar_filter()
        reorder.assert_not_called()
        self.assertEqual(("2", "1"), app.scalar_tree.get_children())

    def test_sort_preserves_numeric_text_stability_selection_and_focus(self) -> None:
        app = self.app
        tree = app.scalar_tree
        for iid, value in (("a", "1,000"), ("b", "20"), ("c", "alpha"), ("d", "ALPHA"), ("e", "Beta")):
            tree.insert("", gui.tk.END, iid=iid, values=("", "", "", value, "", ""))
        tree.selection_set(("b", "d"))
        tree.focus("d")
        with patch.object(tree, "move", side_effect=AssertionError("Quadratic row moves")):
            app._sort_tree(tree, "value")
            self.assertEqual(("e", "c", "d", "a", "b"), tree.get_children())
            app._sort_tree(tree, "value")
            self.assertEqual(("b", "a", "c", "d", "e"), tree.get_children())
        self.assertEqual({"b", "d"}, set(tree.selection()))
        self.assertEqual("d", tree.focus())
        self.assertEqual(("value", False), app.sort_state[id(tree)])

    def test_saved_sort_handles_empty_and_single_row_results(self) -> None:
        app = self.app
        entry = editor.Entry(0, 100, "Health", "FloatProperty", 200, "ActorName=Battery")
        app.document = SimpleNamespace(entries=[entry])
        app.sort_state[id(app.scalar_tree)] = ("name", False)
        with patch.object(app.scalar_tree, "move", side_effect=AssertionError("Quadratic row moves")):
            for query, expected in (("missing", ()), ("health", ("0",)), ("-health", ())):
                app.scalar_filter.set(query)
                app._apply_scalar_filter()
                self.assertEqual(expected, app.scalar_tree.get_children())
            app._clear_scalar_filter()
        self.assertEqual(("0",), app.scalar_tree.get_children())

    def test_other_tables_use_same_bulk_sort(self) -> None:
        app = self.app
        for tree, column in ((app.inventory_tree, "count"), (app.resource_tree, "value"),
                             (app.progress_tree, "value"), (app.backup_tree, "size")):
            with self.subTest(column=column, tree=tree):
                for iid, value in (("a", "2"), ("b", "10")):
                    tree.insert("", gui.tk.END, iid=iid)
                    tree.set(iid, column, value)
                with patch.object(tree, "move", side_effect=AssertionError("Quadratic row moves")):
                    app._sort_tree(tree, column)
                self.assertEqual(("b", "a"), tree.get_children())


class ResourceAmountGuiTests(unittest.TestCase):
    def test_filter_wildcards_and_exclusions(self):
        matches = gui.LastCaretakerEditor._matches_filter
        for query, row, expected in (
            ("electricity battery -NPC", "Electricity BP_Module_Battery", True),
            ("electricity battery -NPC", "Electricity BP_NPC_Battery", False),
            ("electricity battery -NPC", "Electricity Tank", False),
            ("electric* bat?ery -npc*", "ELECTRICITY Battery", True),
            ("-npc* -diesel", "Petrol Battery", True),
            ("-npc* -diesel", "Diesel Battery", False),
            ("battery -bat*ery", "Battery", False),
            ("", "anything", True),
            ("  ", "", True),
            ("*", "", True),
            ("-", "actor_C-879", True),
        ):
            with self.subTest(query=query, row=row):
                self.assertEqual(expected, matches(query, row))

    def test_resource_selection_uses_existing_single_and_bulk_edit_paths(self):
        from unittest.mock import Mock
        entries = [editor.Entry(i, i, "Resources.Diesel", "DoubleProperty", 1) for i in (1, 2)]
        app = SimpleNamespace(
            resource_tree=Mock(), resource_rows={str(e.offset): e for e in entries},
            _prompt_entry=Mock(), _prompt_entries=Mock(),
        )
        app.resource_tree.selection.return_value = ("1",)
        gui.LastCaretakerEditor._edit_resources(app)
        app._prompt_entry.assert_called_once_with(entries[0])
        app.resource_tree.selection.return_value = ("1", "2")
        gui.LastCaretakerEditor._edit_resources(app)
        app._prompt_entries.assert_called_once_with(entries)

    def test_resource_tab_filter_pending_values_and_refresh(self):
        from unittest.mock import Mock
        with (
            patch.object(gui, "_load_settings", return_value={}),
            patch.object(gui.LastCaretakerEditor, "_refresh_saves"),
            patch.object(gui.LastCaretakerEditor, "_load_startup_save"),
            patch.object(gui.LastCaretakerEditor, "_open_full_screen"),
        ):
            try:
                app = gui.LastCaretakerEditor()
            except gui.tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        try:
            entry = editor.Entry(0, 100, "Resources.Diesel", "DoubleProperty", 2000)
            record = editor.ResourceAmount("Diesel", "Boat Petrol Container", "", "Other", 50, entry)
            app.document = SimpleNamespace(
                path=Path("resources.sav"), entries=[entry], resources=[record], items=[],
            )
            world = editor.ResourceAmount(
                "Electricity", "BP_Module_Battery_C_UAID_123", "", "Other", 60,
                editor.Entry(1, 200, "Resources.Electricity", "DoubleProperty", 1),
            )
            npc = editor.ResourceAmount(
                "Electricity", "BP_NPC_Shark_C-88040454", "", "Other", 70,
                editor.Entry(4, 400, "Resources.Electricity", "DoubleProperty", 100),
            )
            app.document.resources.extend([world, npc])
            health = editor.Entry(2, 300, "Health", "FloatProperty", 199.584442)
            damage = editor.Entry(3, 320, "ExtraDamage", "FloatProperty", 0)
            record.health, record.extra_damage = health, damage
            app.document.entries.extend([health, damage])
            app._populate_resources()
            self.assertEqual(("100",), app.resource_tree.get_children())
            self.assertEqual(gui._format_value(health.value), app.resource_tree.set("100", "health"))
            app._populate_scalars()
            self.assertIn("Container Resources", [app.notebook.tab(t, "text") for t in app.notebook.tabs()])
            self.assertIn(app.resource_tree, app._trees())
            app._queue(entry, "3000")
            self.assertEqual("3000", app.resource_tree.set("100", "value"))
            self.assertEqual("3000", app.scalar_tree.set("0", "value"))
            app.resource_filter.set("die* boat -NPC")
            app._apply_resource_filter()
            self.assertEqual(("100",), app.resource_tree.get_children())
            app.resource_filter.set("diesel -boat")
            app._apply_resource_filter()
            self.assertEqual((), app.resource_tree.get_children())
            app.resource_filter.set("missing")
            app._apply_resource_filter()
            self.assertEqual((), app.resource_tree.get_children())
            app.resource_filter.set("199.584 -missing")
            app._apply_resource_filter()
            self.assertEqual(("100",), app.resource_tree.get_children())
            app._clear_resource_filter()
            self.assertEqual(("100",), app.resource_tree.get_children())
            app.resource_tree.selection_set("100")
            app._prompt_entry = Mock()
            app._edit_resources()
            app._prompt_entry.assert_called_once_with(entry)
            app._prompt_entry.reset_mock()
            app._edit_resource_condition("health")
            app._prompt_entry.assert_called_once_with(health)
            app._queue(health, "200")
            self.assertEqual("200", app.resource_tree.set("100", "health"))
            app._queue(damage, "1")
            self.assertEqual("1", app.resource_tree.set("100", "damage"))
            self.assertTrue(app.refill_storage_var.get())
            self.assertTrue(app.refill_rocket_var.get())
            app.confirm_purge_var.set(False)
            app.confirm_repair_var.set(False)
            app.refill_storage_var.set(False)
            app.refill_rocket_var.set(False)
            app.repair_targets["rocket.methane"] = 2500
            app.repair_targets["storage.tanks"] = 31000
            with tempfile.TemporaryDirectory() as folder:
                settings_path = Path(folder) / "settings.json"
                with patch.object(gui, "SETTINGS_PATH", settings_path):
                    self.assertTrue(app._write_settings())
                    settings = gui._load_settings()
                self.assertFalse(settings["refill_storage_tanks"])
                self.assertFalse(settings["refill_rocket_resources"])
                self.assertEqual(2500, settings["repair_stack_targets"]["rocket.methane"])
                self.assertFalse(settings["confirm_purge"])
                self.assertFalse(settings["confirm_repair"])
                self.assertEqual(31000, settings["repair_stack_targets"]["storage.tanks"])
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "resources.sav"
                path.write_bytes(editor.compress_container(ResourceAmountTests()._raw()))
                app.pending.clear()
                self.assertTrue(app._load(path, confirm_discard=False))
                diesel = app.document.resources[0].entry
                app._queue(diesel, "3000")
                with patch.object(gui, "_game_is_running", return_value=False):
                    self.assertTrue(app._save(confirm=False))
                self.assertEqual({}, app.pending)
                self.assertEqual(3000, app.document.resources[0].entry.value)
                self.assertEqual("3000", app.resource_tree.set(str(diesel.offset), "value"))
                self.assertEqual(3000, editor.load_save(path).resources[0].entry.value)
        finally:
            app.destroy()

    def test_inventory_and_scalar_filters_are_independent(self):
        from unittest.mock import Mock
        with (
            patch.object(gui, "_load_settings", return_value={}),
            patch.object(gui.LastCaretakerEditor, "_refresh_saves"),
            patch.object(gui.LastCaretakerEditor, "_load_startup_save"),
            patch.object(gui.LastCaretakerEditor, "_open_full_screen"),
        ):
            try:
                app = gui.LastCaretakerEditor()
            except gui.tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        try:
            item = editor.InventoryItem(
                1, "Back Battery Large", "Equipment", "BP_Module_Battery_C-87904259",
                editor.LOC_BACKPACK, 10, {
                    "ItemCount": editor.Entry(0, 11, "ItemCount", "IntProperty", 1),
                    "Health": editor.Entry(1, 12, "Health", "FloatProperty", 200),
                },
            )
            scalar = editor.Entry(
                0, 100, "Health", "FloatProperty", 200,
                "ActorName=BP_Module_Battery_C-87904259 | Category=Equipment",
            )
            app.document = SimpleNamespace(
                items=[item], entries=[scalar], resources=[], path=Path("filter.sav"),
            )
            app.location_filter.set("All")
            app.item_filter.set("battery equip* 87904259 -NPC")
            app._apply_item_filter()
            self.assertEqual(("1",), app.inventory_tree.get_children())
            app.item_filter.set("87904259 1 bat?ery")
            app._apply_item_filter()
            self.assertEqual(("1",), app.inventory_tree.get_children())
            app.item_filter.set("battery -equip*")
            app._apply_item_filter()
            self.assertEqual((), app.inventory_tree.get_children())
            app.item_filter.set("missing")
            app._apply_item_filter()
            self.assertEqual((), app.inventory_tree.get_children())
            app._clear_item_filter()
            self.assertEqual(("1",), app.inventory_tree.get_children())

            app.scalar_filter.set("health float* Battery_C-879 -NPC")
            app._apply_scalar_filter()
            self.assertEqual(("0",), app.scalar_tree.get_children())
            app.scalar_filter.set("Battery_C-879 200 he?lth")
            app._apply_scalar_filter()
            self.assertEqual(("0",), app.scalar_tree.get_children())
            app.scalar_filter.set("health -bat*")
            app._apply_scalar_filter()
            self.assertEqual((), app.scalar_tree.get_children())
            app.scalar_filter.set("Door")
            app._apply_scalar_filter()
            self.assertEqual((), app.scalar_tree.get_children())
            app._clear_scalar_filter()
            self.assertEqual(("0",), app.scalar_tree.get_children())
        finally:
            app.destroy()


class PlayerStatLabelTests(unittest.TestCase):
    """The player's ``Level`` survival attribute displays as "Charge"."""

    def test_level_rows_display_as_charge(self) -> None:
        for field in ("Current", "Minimum", "Maximum", "Default"):
            self.assertEqual(
                f"Charge - {field}", gui._player_stat_label(f"Level - {field}")
            )

    def test_other_stat_labels_are_unchanged(self) -> None:
        self.assertEqual("Health - Current", gui._player_stat_label("Health - Current"))
        self.assertEqual("Charge - Current", gui._player_stat_label("Charge - Current"))

    def test_caretaker_level_progress_field_is_untouched(self) -> None:
        self.assertIn(("Level", "Caretaker Level"), gui.PROGRESS_FIELDS)


if __name__ == "__main__":
    unittest.main()
