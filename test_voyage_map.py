"""Map parsing, safe relocation, camera math, native controls, and lazy-load coverage."""
from __future__ import annotations

import gc
import json
import math
import struct
import tempfile
import threading
import tkinter as tk
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import extract_named_locations as extractor
import last_caretaker_save_gui as gui
import voyage_gvas as gvas
import voyage_humans as humans
import voyage_map as maps
from save_map_view import SaveMapView
from test_voyage_humans import (
    ALICE, BOB, DOCTOR, END, HEADER, WORLD, document, human, player_actor, pod,
    save, string, struct_map, tag, world,
)


def actor(name, position=(100.0, 200.0, 300.0), *, transform_type="VoyagePackedTransform"):
    return (
        string("ActorName", name, "NameProperty")
        + (tag("Transform", "StructProperty", struct.pack("<3d3f", *position, 0, 0, 0),
               gui.save_io._pack_type(transform_type, gui.save_io._pack_type("/Script/Voyage")))
           if position is not None else b"")
        + END
    )


def packed_transform(name, position):
    return tag(
        name, "StructProperty", struct.pack("<3d3f", *position, 0.0, 0.0, 0.0),
        gui.save_io._pack_type("VoyagePackedTransform", gui.save_io._pack_type("/Script/Voyage")),
    )


def relocatable_actor(name, position, *, attachment=b"\0" * 4):
    component = packed_transform("Transform", position) + END
    component_map = tag(
        "ComponentTransforms", "MapProperty",
        struct.pack("<iiI", 0, 1, 0) + component,
        gui.save_io._pack_type("UInt32Property"),
        gui.save_io._pack_type(
            "StructProperty", gui.save_io._pack_type("PersistentWorldComponentTransform")),
    )
    empty_map = lambda field: tag(
        field, "MapProperty", struct.pack("<ii", 0, 0),
        gui.save_io._pack_type("UInt32Property"),
        gui.save_io._pack_type("StructProperty", gui.save_io._pack_type("TestTransform")),
    )
    pod_state = (
        b"\0" + tag("bHasHumanCurrent", "BoolProperty", b"", flags=0)
        + string("HumanId", "")
        + tag("bHasSeedCurrent", "BoolProperty", b"", flags=0x10) + END + b"\0" * 4
    )
    return (
        string("ActorName", name, "NameProperty")
        + tag("AttachParent", "OptionalProperty", attachment,
              gui.save_io._pack_type("ObjectProperty"))
        + packed_transform("Transform", position)
        + component_map
        + empty_map("ComponentTransformsNonPhysical")
        + empty_map("BoneTransforms")
        + tag("ByteData", "ArrayProperty",
              struct.pack("<i", len(pod_state)) + pod_state,
              gui.save_io._pack_type("ByteProperty"))
        + END
    )


def relocation_save(name="BP_Module_Human_Pod_C123", *, attachment=b"\0" * 4):
    position = (1666900.0, 347200.0, -7188.0)
    target_player = (1666950.0, 347200.0, -7188.0)
    actors = struct_map("actors", [
        struct.pack("<Q", 1) + relocatable_actor(name, position, attachment=attachment),
        struct.pack("<Q", 2) + player_actor(target_player),
    ], "UInt64Property")
    cell = editor_fstring("166:34") + actors + END
    world_data = struct_map("PersistentActors", [cell], "NameProperty") + END
    payload = (
        HEADER + string("BeforeProgress", "preserve")
        + struct_map("GameProgress", [editor_fstring(WORLD) + world_data], "NameProperty")
        + tag("AfterProgress", "BoolProperty", b"", flags=0x10) + END + b"\0" * 4
    )
    return struct.pack("<I", len(payload)) + payload


def editor_fstring(value):
    return gui.save_io._pack_fstring(value)


def warp_world(*, position=(100.25, -200.5, 345.75), cell="pawns",
               attachment=b"\0" * 4, extra_player=False, cached=True,
               component=False, transform_type="VoyagePackedTransform", pawns_cell=None):
    player = (
        string("ActorName", "BP_FirstPersonCharacter_New_C1", "NameProperty")
        + tag("AttachParent", "OptionalProperty", attachment, gui.save_io._pack_type("ObjectProperty"))
        + tag("AttachTransform", "OptionalProperty", b"\0" * 4,
              gui.save_io._pack_type("StructProperty"))
        + tag("Transform", "StructProperty", struct.pack("<3d3f", *position, 10, 20, 30),
              gui.save_io._pack_type(transform_type, gui.save_io._pack_type("/Script/Voyage")))
    )
    for field in ("ComponentTransforms", "ComponentTransformsNonPhysical", "BoneTransforms"):
        entries = [struct.pack("<I", 1) + packed_transform("Transform", position) + END] if component else []
        player += struct_map(field, entries, "UInt32Property")
    player += string("UnrelatedPlayerState", "Do not change equipment or rotation") + END
    actors = [struct.pack("<Q", 1) + player]
    if extra_player:
        actors.append(struct.pack("<Q", 2) + player_actor((0, 0, 0)))
    cells = [
        editor_fstring(cell) + struct_map("actors", actors, "UInt64Property") + END,
        editor_fstring("other-cell") + struct_map("actors", [
            struct.pack("<Q", 3) + actor("BP_Module_Building_C1"),
        ], "UInt64Property") + END,
    ]
    if pawns_cell is not None:
        pawn_actors, key_type = [], "UInt64Property"
        if pawns_cell == "duplicate":
            pawn_actors = [struct.pack("<Q", 1) + actor("BP_Module_Building_C9")]
        elif pawns_cell == "incompatible":
            key_type = "UInt32Property"
        empty = editor_fstring("pawns") + struct_map("actors", pawn_actors, key_type) + END
        cells.insert(0 if pawns_cell == "before" else len(cells), empty)
    persistent = struct_map("PersistentActors", cells, "NameProperty")
    location = tag(
        "LastLocation", "StructProperty", struct.pack("<3d", 101, -202, 350),
        gui.save_io._pack_type("Vector", gui.save_io._pack_type("/Script/CoreUObject")),
    ) if cached else b""
    player_data = tag(
        "PlayerData", "StructProperty",
        location + string("UnrelatedPlayerData", "Preserve progression") + END,
        gui.save_io._pack_type("TestStruct"),
    )
    return player_data + persistent + END


def warp_save(**kwargs):
    payload = HEADER + struct_map("GameProgress", [
        editor_fstring(WORLD) + warp_world(**kwargs),
        editor_fstring("other-world") + warp_world(position=(900, 800, 700)),
    ], "NameProperty") + END + b"\0" * 4
    return struct.pack("<I", len(payload)) + payload


class PlayerWarpTests(unittest.TestCase):
    @staticmethod
    def player(raw):
        return maps.read_map(raw).player(WORLD)

    def test_warp_changes_only_player_and_cached_location_keeps_other_world_and_rotation(self):
        raw = warp_save()
        player = self.player(raw)
        destination = (-12000.75, 4567.25, 16.5)
        offsets = maps.player_warp_offsets(raw, player)
        moved = maps.warp_player(raw, player, destination)
        self.assertEqual(len(raw), len(moved))
        self.assertEqual(destination, self.player(moved).position)
        for offset in offsets:
            self.assertEqual(tuple(value * 100 for value in destination),
                             struct.unpack_from("<3d", moved, offset))
        self.assertEqual(struct.pack("<3f", 10, 20, 30), moved[offsets[0] + 24:offsets[0] + 36])
        changed = [i for i, (a, b) in enumerate(zip(raw, moved)) if a != b]
        self.assertTrue(changed)
        self.assertTrue(all(any(offset <= i < offset + 24 for offset in offsets) for i in changed))
        self.assertEqual(maps.read_map(raw).player("other-world"),
                         maps.read_map(moved).player("other-world"))
        self.assertEqual(moved, maps.warp_player(moved, self.player(moved), destination))

    def test_autosave_cell_player_moves_into_pawns_cell_with_position(self):
        for order in ("before", "after"):
            with self.subTest(pawns_cell=order):
                raw = warp_save(cell="73:88", pawns_cell=order)
                player = self.player(raw)
                destination = (6918.25, -13828.75, 4.5)
                moved = maps.warp_player(raw, player, destination)
                self.assertEqual(len(raw), len(moved))
                self.assertEqual(destination, self.player(moved).position)
                root, _ = gvas.read_property_list(moved, gvas.find_root(moved))
                worlds, _ = humans._struct_records(moved, humans._field(root, "GameProgress"))
                world_entry = next(w for w in worlds if w.key == WORLD)
                cells = {
                    cell.key: [maps._actor_name(moved, a) for a in humans._struct_records(
                        moved, humans._field(cell.props, "actors"))[0]]
                    for cell in humans._struct_records(
                        moved, humans._field(world_entry.props, "PersistentActors"))[0]
                }
                self.assertEqual(["BP_FirstPersonCharacter_New_C1"], cells["pawns"])
                self.assertEqual([], cells["73:88"])
                self.assertEqual(["BP_Module_Building_C1"], cells["other-cell"])
                self.assertIn(b"Do not change equipment or rotation", moved)
                self.assertEqual(maps.read_map(raw).player("other-world").position,
                                 maps.read_map(moved).player("other-world").position)
                self.assertEqual(moved, maps.warp_player(moved, self.player(moved), destination))

    def test_autosave_cell_player_without_compatible_pawns_cell_is_rejected(self):
        for order in ("incompatible", "duplicate"):
            with self.subTest(pawns_cell=order):
                raw = warp_save(cell="73:88", pawns_cell=order)
                with self.assertRaisesRegex((ValueError, gvas.GvasError), "pawns"):
                    maps.warp_player(raw, self.player(raw), (1.0, 2.0, 3.0))

    def test_unsafe_or_unsupported_player_layout_is_rejected(self):
        for kwargs, error in (
            ({"attachment": b"\1\0\0\0"}, "attached"),
            ({"extra_player": True}, "unique"),
            ({"cell": "166:34"}, "pawns"),
            ({"cached": False}, "LastLocation"),
            ({"component": True}, "component transform"),
        ):
            with self.subTest(kwargs=kwargs):
                raw = warp_save(**kwargs)
                point = next(p for p in maps.read_map(raw).points if p.layer == "player" and p.world == WORLD)
                with self.assertRaises((ValueError, gvas.GvasError)) as caught:
                    maps.warp_player(raw, point, (0, 0, 0))
                self.assertIn(error, str(caught.exception))

    def test_stale_identity_position_nonplayer_and_invalid_coordinates_are_rejected(self):
        from dataclasses import replace
        raw = warp_save()
        player = self.player(raw)
        for point in (
            replace(player, actor_offset=0),
            replace(player, actor="BP_FirstPersonCharacter_wrong"),
            replace(player, position=(0, 0, 0)),
            replace(player, world="missing"),
            replace(player, layer="locations"),
        ):
            with self.subTest(point=point), self.assertRaises(ValueError):
                maps.warp_player(raw, point, (0, 0, 0))
        for destination in ((math.nan, 0, 0), (0, math.inf, 0), (0, 0, -math.inf),
                            (1e308, 0, 0), (0, 0), ("bad", 0, 0)):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                maps.warp_player(raw, player, destination)

    def test_backed_up_atomic_write_and_stale_file_rejection(self):
        raw = warp_save()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "warp.sav"
            original = gui.save_io.compress_container(raw)
            path.write_bytes(original)
            doc = gui.save_io.load_save(path)
            with patch.object(gui, "_game_is_running", return_value=False):
                backup = gui.LastCaretakerEditor._save_player_warp(doc, self.player(raw), (30, 40, 50), 3)
            self.assertIsNotNone(backup)
            self.assertEqual(original, backup.read_bytes())
            updated = gui.save_io.load_save(path)
            self.assertEqual((30, 40, 50), self.player(updated.raw).position)
            current = path.read_bytes()
            with patch.object(gui, "_game_is_running", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                    gui.LastCaretakerEditor._save_player_warp(doc, self.player(raw), (60, 70, 80), 3)
            self.assertEqual(current, path.read_bytes())

    def test_failed_backup_prevents_write_and_game_running_is_refused(self):
        raw = warp_save()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "warp.sav"
            original = gui.save_io.compress_container(raw)
            path.write_bytes(original)
            doc = gui.save_io.load_save(path)
            with (
                patch.object(gui, "_game_is_running", return_value=False),
                patch.object(gui.save_io, "backup_save", side_effect=OSError("backup denied")),
            ):
                with self.assertRaisesRegex(OSError, "backup denied"):
                    gui.LastCaretakerEditor._save_player_warp(doc, self.player(raw), (1, 2, 3), 3)
            self.assertEqual(original, path.read_bytes())
            with patch.object(gui, "_game_is_running", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "Close"):
                    gui.LastCaretakerEditor._save_player_warp(doc, self.player(raw), (1, 2, 3), 3)
            self.assertEqual(original, path.read_bytes())

    def test_coordinate_dialog_rejects_invalid_values_for_each_axis(self):
        for axis in ("X", "Y", "Z"):
            for value in ("nan", "inf", "-inf", "1e308", "oops", ""):
                with self.subTest(axis=axis, value=value), patch.object(
                    gui.messagebox, "showerror",
                ) as error:
                    entries = {
                        name: Mock(get=Mock(return_value=value if name == axis else "12.5"))
                        for name in ("X", "Y", "Z")
                    }
                    dialog = SimpleNamespace(coordinate_entries=entries, result=None)
                    self.assertFalse(gui._PlayerWarpDialog.validate(dialog))
                    error.assert_called_once_with(
                        "Invalid coordinate", f"Enter a finite {axis} coordinate in meters.",
                        parent=dialog,
                    )
                    entries[axis].focus_set.assert_called_once()
                    entries[axis].selection_range.assert_called_once_with(0, tk.END)
                    self.assertIsNone(dialog.result)
        entries = {
            axis: Mock(get=Mock(return_value=value))
            for axis, value in zip(("X", "Y", "Z"), ("12.5", "-3.075e1", "-18.25"))
        }
        dialog = SimpleNamespace(coordinate_entries=entries)
        self.assertTrue(gui._PlayerWarpDialog.validate(dialog))
        self.assertEqual((12.5, -30.75, -18.25), dialog.result)


class MapReaderTests(unittest.TestCase):
    def test_exact_pod_coordinates_units_identity_and_height_aware_distance(self):
        position = (697005.1184324448, -16863.877727958843, 11213.787262575763)
        raw = save([human(BOB, (DOCTOR,), result="Success")],
                   [pod(BOB, (DOCTOR,), position=position)],
                   player_position=(position[0], position[1], position[2] - 1000))
        data = maps.read_map(raw, humans.read_humans(raw))
        point = next(point for point in data.points if point.layer == "humans")
        self.assertEqual(tuple(value / 100 for value in position), point.position)
        self.assertEqual(BOB, point.social_id)
        self.assertEqual("Doctor", point.profession)
        self.assertEqual(10.0, data.distance(point))
        self.assertEqual([WORLD], list(data.worlds))
        self.assertEqual(raw, save([human(BOB, (DOCTOR,), result="Success")],
                                  [pod(BOB, (DOCTOR,), position=position)],
                                  player_position=(position[0], position[1], position[2] - 1000)))

    def test_classifies_known_layers_without_guessing_other_actor_types(self):
        raw = save([], [
            pod(BOB, position=(100, 200, 300)),
            pod("", occupied=False, actor="BP_Module_Human_Pod_C2", position=(200, 200, 300)),
            actor("BP_Rocket_Functional_C_UAID_123"),
            actor("BP_Module_Checkpoint_C_UAID_123"),
            actor("BP_Inventory_Container_Large_C_123"),
            actor("BP_Inventory_PlayerBox_Large_C-123"),
            actor("BP_EggSack01_C_UAID_123"),
        ], player_position=(0, 0, 0))
        data = maps.read_map(raw)
        self.assertEqual(
            {"player": 1, "humans": 1, "pods": 1, "rockets": 1, "checkpoints": 1,
             "containers": 2, "other": 1},
            dict(Counter(point.layer for point in data.points)),
        )
        self.assertIn("no grown human", next(p.label for p in data.points if p.layer == "pods"))

    def test_unavailable_positions_are_counted_not_invented(self):
        raw = save([], [
            actor("BP_Unknown_C1", None),
            actor("BP_Unknown_C2", (math.nan, 0, 0)),
            actor("BP_Unknown_C3", (math.inf, 0, 0)),
            actor("BP_Unknown_C4", transform_type="UnknownTransform"),
            actor("BP_Unknown_C5"),
        ])
        data = maps.read_map(raw)
        self.assertEqual(5, data.actor_count)
        self.assertEqual(4, data.skipped_positions)
        self.assertEqual(1, len(data.points))

    def test_unreadable_pod_state_is_reported_without_claiming_occupancy(self):
        data = maps.read_map(save([], [actor("BP_Module_Human_Pod_C1")]))
        self.assertEqual("other", data.points[0].layer)
        self.assertEqual("Human pod (state unavailable)", data.points[0].label)
        self.assertEqual(1, len(data.warnings))
        self.assertIn("ByteData", data.warnings[0])

    def test_map_does_not_require_a_human_roster(self):
        data = maps.read_map(save([], [actor("BP_Unknown_C1")], include_roster=False))
        self.assertEqual(1, len(data.points))

    def test_player_distances_are_world_specific_and_ambiguous_players_are_unavailable(self):
        raw = save([], [actor("BP_Unknown_C1")], player_position=(0, 0, 0),
                   extra_worlds=(("world2", world([], [actor("BP_Unknown_C2")],
                                                   player_position=(100, 200, 300))),))
        data = maps.read_map(raw)
        other = next(p for p in data.points if p.world == "world2" and p.layer == "other")
        self.assertEqual(0, data.distance(other))
        raw = save([], [player_actor((0, 0, 0)),
                        actor("BP_FirstPersonCharacter_New_C2", (100, 0, 0)),
                        actor("BP_Unknown_C1")])
        data = maps.read_map(raw)
        self.assertIsNone(data.player(WORLD))
        self.assertIsNone(data.distance(data.points[-1]))

    def test_invalid_save_is_rejected(self):
        for raw in (b"", b"\0" * 8, save()[:-1]):
            with self.subTest(length=len(raw)), self.assertRaises(gvas.GvasError):
                maps.read_map(raw)

    def test_progress_reports_work_without_touching_tk(self):
        progress = Mock()
        maps.read_map(save([], [actor("BP_Unknown_C1")]), progress=progress)
        self.assertIn("Reading map positions", progress.call_args.args[0])

    def test_loose_pod_move_updates_actor_and_component_without_changing_egg_state(self):
        raw = relocation_save()
        data = maps.read_map(raw)
        point = next(point for point in data.points if point.layer == "pods")
        self.assertEqual("", point.move_block_reason)
        destination = (16669.5, 3471.0, -71.88)
        moved = maps.relocate_actor(raw, point, destination)
        self.assertNotEqual(raw, moved)
        updated = maps.read_map(moved)
        relocated = next(item for item in updated.points if item.actor == point.actor)
        self.assertEqual(destination, relocated.position)
        self.assertEqual(point.position, (16669.0, 3472.0, -71.88))
        changed = [index for index, (old, new) in enumerate(zip(raw, moved)) if old != new]
        root, _ = gvas.read_property_list(raw, gvas.find_root(raw))
        world_entry = humans._struct_records(raw, humans._field(root, "GameProgress"))[0][0]
        cell = humans._struct_records(raw, humans._field(world_entry.props, "PersistentActors"))[0][0]
        actor_entry = humans._struct_records(raw, humans._field(cell.props, "actors"))[0][0]
        actor_transform = humans._field(actor_entry.props, "Transform")
        component_entry = humans._map_entries(
            raw, humans._field(actor_entry.props, "ComponentTransforms"))[0]
        component_props, _ = gvas.read_property_list(raw, component_entry.value_start)
        component_transform = humans._field(component_props, "Transform")
        transform_ranges = (
            (actor_transform.payload, actor_transform.payload + 24),
            (component_transform.payload, component_transform.payload + 24),
        )
        self.assertTrue(changed)
        self.assertTrue(all(any(start <= index < end for start, end in transform_ranges)
                            for index in changed))
        moved_root, _ = gvas.read_property_list(moved, gvas.find_root(moved))
        moved_world = humans._struct_records(
            moved, humans._field(moved_root, "GameProgress"))[0][0]
        moved_cell = humans._struct_records(
            moved, humans._field(moved_world.props, "PersistentActors"))[0][0]
        moved_actor = humans._struct_records(
            moved, humans._field(moved_cell.props, "actors"))[0][0]
        blob = humans._field(moved_actor.props, "ByteData")
        props = humans._blob_properties(moved, blob)
        self.assertTrue(humans._has_human(moved, humans._field(props, "bHasSeedCurrent")))

    def test_move_rejects_buildings_attached_objects_and_cell_changes(self):
        building = maps.read_map(relocation_save("BP_Module_Building_C123")).points[0]
        self.assertIn("Only loose human seed items", building.move_block_reason)
        with self.assertRaisesRegex(ValueError, "Only loose human seed items"):
            maps.relocate_actor(relocation_save("BP_Module_Building_C123"), building,
                                (16669.5, 3471.0, -71.88))

        attached_raw = relocation_save(attachment=b"\1\0\0\0")
        attached = next(point for point in maps.read_map(attached_raw).points if point.layer == "pods")
        self.assertIn("attached", attached.move_block_reason)
        with self.assertRaisesRegex(ValueError, "attached"):
            maps.relocate_actor(attached_raw, attached, (16669.5, 3471.0, -71.88))

        raw = relocation_save()
        pod_point = next(point for point in maps.read_map(raw).points if point.layer == "pods")
        with self.assertRaisesRegex(ValueError, "cell boundary"):
            maps.relocate_actor(raw, pod_point, (16700.0, 3471.0, -71.88))


class NamedLocationTests(unittest.TestCase):
    ACTOR = "VoyageLocatorActor_UAID_ABC_123"

    def write(self, payload):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "named_locations.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return path

    def catalog(self, world_name=WORLD):
        return {"locations": [
            {"actor": self.ACTOR, "world": world_name, "name": "Lazarus Complex",
             "ident": "LazarusComplex", "x": 6954.88, "y": -166.87, "z": 7.38},
            {"actor": "VoyageLocatorActor_UAID_OTHER", "world": "elsewhere", "name": "Far",
             "x": 1, "y": 2, "z": 3},
        ]}

    def test_locations_join_their_world_are_searchable_and_report_save_listing(self):
        path = self.write(self.catalog())
        raw = save([], [actor("BP_Unknown_C1")], player_position=(0, 0, 0))
        data = maps.read_map(raw, named_locations=path)
        places = [point for point in data.points if point.layer == "locations"]
        self.assertEqual(1, len(places))
        place = places[0]
        self.assertEqual(("Lazarus Complex", (6954.88, -166.87, 7.38), self.ACTOR),
                         (place.label, place.position, place.actor))
        self.assertIn("LazarusComplex", place.searchable)
        self.assertIn("In this save's map-marker list: no", place.details)
        self.assertIs(place.in_save, False)
        self.assertEqual("Game files only", place.source)
        self.assertEqual((), data.notices)
        self.assertEqual(2, len([p for p in data.points if p.layer != "locations"]))
        with patch.object(maps, "_saved_locators", return_value={self.ACTOR}):
            place = next(p for p in maps.read_map(raw, named_locations=path).points
                         if p.layer == "locations")
        self.assertIn("In this save's map-marker list: yes", place.details)
        self.assertIs(place.in_save, True)
        self.assertEqual("Save + game files", place.source)
        with patch.object(maps, "_saved_locators", return_value=None):
            place = next(p for p in maps.read_map(raw, named_locations=path).points
                         if p.layer == "locations")
        self.assertIn("unknown", " ".join(place.details))
        self.assertIsNone(place.in_save)
        self.assertEqual("Save status unknown", place.source)

    def test_missing_or_invalid_catalog_is_a_notice_not_a_map_failure(self):
        raw = save([], [actor("BP_Unknown_C1")])
        bad = dict(self.catalog())
        bad["locations"] = [dict(bad["locations"][0], x="nan")]
        for path in (Path(tempfile.gettempdir()) / "missing-named-locations.json",
                     self.write("{not json"), self.write({"nope": []}), self.write(bad)):
            with self.subTest(path=path.name):
                data = maps.read_map(raw, named_locations=path)
                self.assertEqual(1, len(data.points))
                self.assertEqual(1, len(data.notices))
                self.assertIn("Named locations unavailable", data.notices[0])

    def test_map_without_catalog_has_no_locations(self):
        data = maps.read_map(save([], [actor("BP_Unknown_C1")]))
        self.assertFalse(any(point.layer == "locations" for point in data.points))

    def test_shipped_catalog_is_valid_and_matches_known_places(self):
        locations = {location.name: location for location in maps.load_named_locations()}
        self.assertGreaterEqual(len(maps.load_named_locations()), 100)
        for name, (x, y) in {"Lazarus Complex": (6954.9, -166.9), "Exodus Station": (12579.3, 4811.7),
                             "Platform Omega": (8917.7, 597.8), "Unlisted Tower": (11286.9, -2074.5)}.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(x, locations[name].position[0], delta=1)
                self.assertAlmostEqual(y, locations[name].position[1], delta=1)
                self.assertEqual(WORLD, locations[name].world)

    def test_location_types_use_identifiers_when_display_names_hide_the_type(self):
        examples = (
            ("Cave01", "First Whisper", "caves"),
            ("Cave04", "North Lantern", "caves"),
            ("GyrocopyterPlatformA", "Gyrocopyter Platform A", "gyrocopter"),
            ("GyrocopterTower", "Gyrocopter tower", "gyrocopter"),
            ("UnlistedTower", "Unlisted Tower", "towers"),
            ("LazarusComplex", "Lazarus Complex", "stations"),
            ("StarChildA", "Courier Sieve Node Facility", "stations"),
            ("RefuelOutpostA", "Refuel Outpost Gamma", "outposts"),
            ("SubmergedLab", "Research Outpost Theta-9", "outposts"),
            ("SeedReserve_01", "Seed Reserve 01", "reserves"),
            ("SeedVaultA", "Seed Vault Prometheus", "reserves"),
            ("HeliosReserveA", "Helios Reserve Orion", "reserves"),
            ("HabitatNode01", "Habitat Node 01-2.5", "habitats"),
            ("GrowthContainer03", "NDNs Node -10,-2", "growth_nodes"),
            ("YorkGrave", "York Grave", "graves"),
            ("SanctuaryDock12", "Sanctuary Dock 12", "docks"),
            ("Platform_Omega", "Platform Omega", "docks"),
            ("SiloA", "MCV-12 Eternal Flame", "silos"),
            ("QuarantineStackAmen", "Quarantine Stack Amen", "quarantine"),
            ("NavigationBeacon15", "Navigation Beacon 15", "beacons"),
            ("Maze1", "Transposium", "landmarks"),
            ("FuturePlace", "Future place", "landmarks"),
        )
        for ident, name, expected in examples:
            with self.subTest(ident=ident):
                self.assertEqual(expected, maps.named_location_type(ident, name))
                self.assertEqual(expected, maps.named_location_type(ident.upper(), name.upper()))

    def test_types_reach_map_points_search_and_colors_for_the_entire_catalog(self):
        raw = save([], [], player_position=(0, 0, 0))
        data = maps.read_map(raw, named_locations=maps.NAMED_LOCATIONS_PATH)
        sites = [point for point in data.points if point.layer == "locations"]
        counts = Counter(point.location_type for point in sites)
        self.assertEqual(set(maps.LOCATION_TYPE_NAMES), set(counts))
        for point in sites:
            self.assertEqual(maps.LOCATION_TYPE_NAMES[point.location_type], point.type_label)
            self.assertEqual(maps.LOCATION_TYPE_COLORS[point.location_type], point.color)
            self.assertIn(point.type_label, point.searchable)
        cave = next(point for point in sites if point.label == "First Whisper")
        self.assertEqual("caves", cave.location_type)
        self.assertTrue(gui.LastCaretakerEditor._matches_filter("caves", cave.searchable))
        player = data.player(WORLD)
        self.assertEqual(maps.LAYER_COLORS["player"], player.color)
        self.assertEqual("Player", player.type_label)

    def test_location_colors_are_distinct_and_readable_on_map_and_list_backgrounds(self):
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                      for v in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        self.assertEqual(len(maps.LOCATION_TYPES), len(set(maps.LOCATION_TYPE_COLORS.values())))
        for key, _label, color in maps.LOCATION_TYPES:
            for background in ("#ffffff", "#eef3f6"):
                with self.subTest(kind=key, background=background):
                    self.assertGreaterEqual(
                        (luminance(background) + 0.05) / (luminance(color) + 0.05), 4.5)

    def test_extractor_parses_single_and_grouped_locator_names(self):
        def fstring(text):
            return struct.pack("<i", len(text) + 1) + text.encode() + b"\0"

        tail = (b"\0\0\0\0\x0b" + b"\x04" + b"\0" * 7 + fstring("Location_LazarusComplex")
                + bytes.fromhex("0104030050c346b5fcffff") + struct.pack("<3d", 695488.0, -16687.0, -3e-7)
                + struct.pack("<3d", 0, 90, 0))
        table = {"Location_LazarusComplex": "Lazarus Complex"}
        for names in (("LazarusComplex", "LazarusComplex"), ("Cluster1", "LazarusComplex")):
            with self.subTest(names=names):
                parsed = extractor._parse_locator(b"\0\x06\x02" + b"".join(map(fstring, names)) + tail, table)
                self.assertEqual({"ident": "LazarusComplex", "key": "Location_LazarusComplex",
                                  "name": "Lazarus Complex", "x": 6954.88, "y": -166.87, "z": -0.0},
                                 parsed)
        parsed = extractor._parse_locator(b"\0" + fstring("NoTableEntry") * 2 + tail, {})
        self.assertEqual("No Table Entry", parsed["name"])


class MapCameraTests(unittest.TestCase):
    def test_focus_centers_exactly_at_maximum_zoom_and_obeys_the_zoom_cap(self):
        camera = maps.MapCamera(-500, 300, 0.001)
        target = (6918.06, -13828.75, 2.56)
        camera.focus(target)
        self.assertEqual(target[:2], (camera.x, camera.y))
        self.assertEqual(maps.MapCamera.MAX_SCALE, camera.scale)
        self.assertEqual((400, 300), camera.screen(*target[:2], 800, 600))
        camera.zoom(2, 400, 300, 800, 600)
        self.assertEqual(maps.MapCamera.MAX_SCALE, camera.scale)
        camera.zoom(0.5, 400, 300, 800, 600)
        camera.pan(100, 100)
        camera.focus(target)
        self.assertEqual(target[:2], (camera.x, camera.y))
        self.assertEqual(maps.MapCamera.MAX_SCALE, camera.scale)

    def test_roundtrip_orientation_pan_and_cursor_anchored_zoom(self):
        camera = maps.MapCamera(123, -456, 2)
        pixel = camera.screen(500, 300, 800, 600)
        self.assertEqual((500, 300), camera.world(*pixel, 800, 600))
        self.assertGreater(pixel[0], 400)
        # North is -Y, so a larger (more southern) Y is lower on screen.
        self.assertGreater(pixel[1], 300)
        north = camera.screen(123, -1000, 800, 600)
        south = camera.screen(123, 1000, 800, 600)
        self.assertLess(north[1], south[1])
        before = camera.world(100, 150, 800, 600)
        camera.zoom(3, 100, 150, 800, 600)
        self.assertEqual(before, camera.world(100, 150, 800, 600))
        camera.pan(60, -60)
        self.assertEqual((before[0] - 10, before[1] + 10), camera.world(100, 150, 800, 600))

    def test_named_compass_places_render_north_up(self):
        named = {item.name: item.position for item in maps.load_named_locations(maps.NAMED_LOCATIONS_PATH)}
        camera = maps.MapCamera(6000, -5000, 0.01)
        north = camera.screen(*named["North Lantern"][:2], 800, 600)
        south = camera.screen(*named["South Echo"][:2], 800, 600)
        self.assertLess(north[1], south[1])

    def test_fit_handles_empty_single_coincident_and_large_ranges(self):
        for positions in ([], [(1, 2, 3)], [(1, 2, 3), (1, 2, 9)],
                          [(-20000, -5000, 0), (20000, 5000, 0)]):
            with self.subTest(positions=positions):
                camera = maps.MapCamera()
                camera.fit(positions, 800, 600)
                self.assertGreater(camera.scale, 0)
                for position in positions:
                    x, y = camera.screen(*position[:2], 800, 600)
                    self.assertTrue(0 <= x <= 800)
                    self.assertTrue(0 <= y <= 600)


class MapViewTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("1040x650")
        self.addCleanup(self.root.destroy)
        self.view = SaveMapView(self.root, reload_map=Mock(),
                                matches_filter=gui.LastCaretakerEditor._matches_filter)
        self.view.pack(fill=tk.BOTH, expand=True)
        self.root.update()
        self.points = (
            maps.MapPoint("p", WORLD, "BP_FirstPersonCharacter", "Saved player position",
                          "player", (0, 0, 0)),
            maps.MapPoint("h1", WORLD, "pod1", "Human in pod: AAAAAAAA", "humans",
                          (1000, 2000, 300), ALICE, "Doctor"),
            maps.MapPoint("h2", WORLD, "pod2", "Human in pod: BBBBBBBB", "humans",
                          (1000.5, 2000.5, 310), BOB, "Teacher"),
            maps.MapPoint("c", WORLD, "crate1", "Player crate", "containers", (500, 600, 0)),
            maps.MapPoint("p2", "other-world", "player2", "Saved player position",
                          "player", (100, 100, 0)),
        )
        self.view.set_data(maps.MapData(self.points, (WORLD, "other-world"), 6, 1))
        self.root.update()
        self.root.withdraw()

    def test_site_list_includes_game_only_sites_independent_of_layers_and_map_search(self):
        view = self.view
        sites = (
            maps.MapPoint("l1", WORLD, "Locator1", "Game-only site", "locations",
                          (500, -900, 2), in_save=False),
            maps.MapPoint("l2", WORLD, "Locator2", "Saved site", "locations",
                          (600, -800, 3), in_save=True),
            maps.MapPoint("l3", WORLD, "Locator3", "Unknown site", "locations",
                          (700, -700, 4), in_save=None),
            maps.MapPoint("boat", WORLD, "BP_Boat_Barge_C1031", "Boat Barge", "boats",
                          (1000, -2000, 3)),
        )
        view.set_data(maps.MapData(self.points + sites, (WORLD, "other-world")))
        view.layer_vars["locations"].set(False)
        view.query.set("nothing matches")
        view.apply_filter()
        self.assertEqual(3, len(view.location_rows))
        self.assertEqual(
            ["Game files only", "Save + game files", "Save status unknown"],
            [view.location_tree.set(iid, "source") for iid in view.location_tree.get_children()],
        )
        view.list_query.set("Game-only")
        view._populate_location_list()
        self.assertEqual(["l1"], [p.key for p in view.location_rows.values()])
        view.location_tree.selection_set("0")
        view._show_list_point()
        self.assertEqual("l1", view.selected.key)
        self.assertTrue(view.layer_vars["locations"].get())
        self.assertEqual("", view.query.get())
        self.assertEqual((500, -900), (view.camera.x, view.camera.y))
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)
        self.assertIn("Source: Game files only", view.details.get("1.0", tk.END))
        view.list_scope.set("Boats")
        view.list_query.set("")
        view._populate_location_list()
        self.assertEqual(["boat"], [p.key for p in view.location_rows.values()])
        self.assertEqual("Save", view.location_tree.set("0", "source"))
        view.location_tree.selection_set("0")
        view._show_list_point()
        view.draw()
        self.assertTrue(any(p.key == "boat" for group in view.groups.values() for p in group))
        view.world_var.set("other-world")
        view.apply_filter()
        self.assertEqual({}, view.location_rows)
        view.reset()
        self.assertEqual((), view.location_tree.get_children())

    def test_selecting_a_site_row_automatically_zooms_full_and_keeps_list_open(self):
        view = self.view
        site = maps.MapPoint("cave", WORLD, "CaveLocator", "First Whisper", "locations",
                             (800, -500, 10), location_type="caves", in_save=False)
        view.set_data(maps.MapData((self.points[0], site), (WORLD,)))
        view.layer_vars["locations"].set(False)
        view.query.set("player")
        view.apply_filter()
        view.inspector.select(view.list_tab)
        view._fit_pending = True
        view.location_tree.selection_set("0")
        self.root.update()
        self.assertEqual(site, view.selected)
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)
        self.assertEqual(site.position[:2], (view.camera.x, view.camera.y))
        self.assertFalse(view._fit_pending)
        self.assertEqual(str(view.list_tab), view.inspector.select())
        self.assertEqual(("0",), view.location_tree.selection())
        self.assertEqual("", view.query.get())
        self.assertTrue(view.layer_vars["locations"].get())
        self.assertEqual("Caves", view.location_tree.set("0", "kind"))
        self.assertIn("Type: Caves", view.details.get("1.0", tk.END))

    def test_zoom_full_button_reframes_selected_object_and_reset_disables_it(self):
        view = self.view
        self.assertIn("disabled", view.zoom_full_button.state())
        view._select_group([self.points[1]])
        self.assertNotIn("disabled", view.zoom_full_button.state())
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)
        view.zoom(0.001)
        view._pan(100, 20)
        view.zoom_full_button.invoke()
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)
        self.assertEqual(self.points[1].position[:2], (view.camera.x, view.camera.y))
        view.reset()
        self.assertIn("disabled", view.zoom_full_button.state())

    def test_named_site_colors_match_canvas_rows_and_legend(self):
        view = self.view
        sites = (
            maps.MapPoint("cave", WORLD, "Cave01", "First Whisper", "locations",
                          (0, 0, 1), location_type="caves"),
            maps.MapPoint("gyro", WORLD, "GyroPlatform", "Gyrocopter platform", "locations",
                          (100, 0, 1), location_type="gyrocopter"),
            maps.MapPoint("tower", WORLD, "Tower", "Unlisted Tower", "locations",
                          (200, 0, 1), location_type="towers"),
        )
        view.set_data(maps.MapData(sites, (WORLD,)))
        view.draw()
        for tag, group in view.groups.items():
            point = group[0]
            shapes = [item for item in view.canvas.find_withtag(tag)
                      if view.canvas.type(item) == "rectangle"]
            self.assertEqual(1, len(shapes))
            self.assertEqual(point.color, view.canvas.itemcget(shapes[0], "fill"))
        for iid, point in view.location_rows.items():
            self.assertEqual(point.type_label, view.location_tree.set(iid, "kind"))
            self.assertEqual((point.location_type,), view.location_tree.item(iid, "tags"))
            self.assertEqual(point.color, str(view.location_tree.tag_configure(
                point.location_type, "foreground")))
            self.assertEqual(point.color, view.location_legend[point.location_type].cget("foreground"))
            self.assertEqual(point.color, view.location_legend[point.location_type].cget("activeforeground"))

    def _legend_filter_points(self):
        return (
            self.points[0],
            self.points[3],
            maps.MapPoint("cave1", WORLD, "Cave01", "First Whisper", "locations",
                          (100, 100, 1), location_type="caves", in_save=False),
            maps.MapPoint("cave2", WORLD, "Cave02", "Grey Mouth", "locations",
                          (200, 100, 2), location_type="caves", in_save=True),
            maps.MapPoint("gyro", WORLD, "GyroPlatform", "Gyrocopter platform", "locations",
                          (300, 100, 3), location_type="gyrocopter"),
            maps.MapPoint("tower", WORLD, "Tower", "Unlisted Tower", "locations",
                          (400, 100, 4), location_type="towers"),
            maps.MapPoint("boat", WORLD, "BP_Boat_Barge_C1031", "Boat Barge", "boats",
                          (500, 100, 5)),
            maps.MapPoint("other-cave", "other-world", "Cave03", "Far cave", "locations",
                          (600, 100, 6), location_type="caves"),
        )

    def test_legend_filters_multiple_types_without_changing_other_layer_settings(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        view.layer_vars["locations"].set(False)
        view.layer_vars["containers"].set(True)
        view.apply_filter()
        original_layers = {key: var.get() for key, var in view.layer_vars.items()}
        changed = view.on_layers_changed = Mock()

        view.location_legend["caves"].invoke()
        self.assertEqual({"cave1", "cave2"}, {point.key for point in view.visible})
        self.assertEqual({"caves"}, view._selected_location_types())
        self.assertTrue(view.layer_vars["locations"].get())
        self.assertEqual((150, 100), (view.camera.x, view.camera.y))
        self.assertIn("Location types only: Caves", view.status.get())
        self.assertEqual(tk.NORMAL, str(view.show_all_types_button["state"]))
        changed.assert_called_once_with()
        view.draw()
        self.assertEqual({"cave1", "cave2"},
                         {point.key for group in view.groups.values() for point in group})

        view.location_legend["gyrocopter"].invoke()
        self.assertEqual({"cave1", "cave2", "gyro"}, {point.key for point in view.visible})
        self.assertIn("Caves, Gyrocopter platforms", view.status.get())
        view.location_legend["caves"].invoke()
        self.assertEqual({"gyro"}, {point.key for point in view.visible})
        view.location_legend["gyrocopter"].invoke()
        self.assertEqual(set(), view._selected_location_types())
        self.assertEqual(tk.DISABLED, str(view.show_all_types_button["state"]))
        self.assertEqual({"p", "c", "cave1", "cave2", "gyro", "tower", "boat"},
                         {point.key for point in view.visible})
        for key, was_enabled in original_layers.items():
            if key != "locations":
                self.assertEqual(was_enabled, view.layer_vars[key].get(), key)
        changed.assert_called_once_with()

    def test_show_all_clear_reset_and_layer_toggle_remove_the_type_filter(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        view.query.set("First Whisper")
        view.location_legend["caves"].invoke()
        self.assertEqual(["cave1"], [point.key for point in view.visible])
        view.show_all_types_button.invoke()
        self.assertEqual("First Whisper", view.query.get())
        self.assertEqual(set(), view._selected_location_types())
        self.assertEqual(["cave1"], [point.key for point in view.visible])

        view.location_legend["caves"].invoke()
        view.location_legend["towers"].invoke()
        view.clear_filter()
        self.assertEqual("", view.query.get())
        self.assertEqual(set(), view._selected_location_types())
        self.assertIn("p", {point.key for point in view.visible})

        view.location_legend["caves"].invoke()
        view.layer_checks["locations"].invoke()
        self.assertFalse(view.layer_vars["locations"].get())
        self.assertEqual(set(), view._selected_location_types())
        self.assertIn("p", {point.key for point in view.visible})
        self.assertNotIn("cave1", {point.key for point in view.visible})

        view.location_legend["caves"].invoke()
        view.reset()
        self.assertEqual(set(), view._selected_location_types())
        self.assertEqual(tk.DISABLED, str(view.show_all_types_button["state"]))

    def test_legend_filter_keeps_world_and_search_filters_and_reports_empty_results(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        view.location_legend["caves"].invoke()
        view.query.set("Grey")
        view.apply_filter()
        self.assertEqual(["cave2"], [point.key for point in view.visible])
        view.world_var.set("other-world")
        view.apply_filter(fit=True)
        self.assertEqual([], view.visible)
        self.assertIn("0 matching objects", view.status.get())
        self.assertIn("Location types only: Caves", view.status.get())
        view.query.set("")
        view.apply_filter()
        self.assertEqual(["other-cave"], [point.key for point in view.visible])

    def test_not_visited_legend_shows_only_game_files_only_and_enables_locations(self):
        view = self.view
        extra = (
            maps.MapPoint("unknown", WORLD, "CaveUnknown", "Unknown cave", "locations",
                          (700, 100, 1), location_type="caves", in_save=None),
            maps.MapPoint("unvisited-tower", WORLD, "TowerMissing", "Missing tower", "locations",
                          (800, 100, 2), location_type="towers", in_save=False),
        )
        view.set_data(maps.MapData(self._legend_filter_points() + extra, (WORLD, "other-world")))
        view.layer_vars["locations"].set(False)
        view.layer_vars["containers"].set(True)
        view.apply_filter()
        original_layers = {key: var.get() for key, var in view.layer_vars.items()}
        changed = view.on_layers_changed = Mock()

        self.assertFalse(view.not_visited_var.get())
        self.assertEqual("Not visited (game files only)", view.not_visited_check.cget("text"))
        view.not_visited_check.invoke()
        self.assertEqual({"cave1", "unvisited-tower"}, {point.key for point in view.visible})
        self.assertEqual({"Game files only"}, {point.source for point in view.visible})
        self.assertTrue(view.layer_vars["locations"].get())
        self.assertIn("Not visited (game files only)", view.status.get())
        self.assertEqual(tk.DISABLED, str(view.show_all_types_button["state"]))
        for key, was_enabled in original_layers.items():
            if key != "locations":
                self.assertEqual(was_enabled, view.layer_vars[key].get(), key)
        changed.assert_called_once_with()

        view.not_visited_check.invoke()
        self.assertFalse(view.not_visited_var.get())
        self.assertTrue({"p", "cave1", "cave2", "unknown", "boat"} <=
                        {point.key for point in view.visible})
        self.assertNotIn("Not visited (game files only)", view.status.get())
        changed.assert_called_once_with()

    def test_not_visited_intersects_types_and_respects_search_and_world(self):
        view = self.view
        extra = (
            maps.MapPoint("unvisited-tower", WORLD, "TowerMissing", "Missing tower", "locations",
                          (800, 100, 2), location_type="towers", in_save=False),
            maps.MapPoint("other-unvisited", "other-world", "CaveMissing", "Other cave",
                          "locations", (900, 100, 3), location_type="caves", in_save=False),
        )
        view.set_data(maps.MapData(self._legend_filter_points() + extra, (WORLD, "other-world")))
        view.location_legend["caves"].invoke()
        view.not_visited_check.invoke()
        self.assertEqual({"cave1"}, {point.key for point in view.visible})
        view.location_legend["towers"].invoke()
        self.assertEqual({"cave1", "unvisited-tower"}, {point.key for point in view.visible})
        view.show_all_types_button.invoke()
        self.assertEqual(set(), view._selected_location_types())
        self.assertTrue(view.not_visited_var.get())
        self.assertEqual({"cave1", "unvisited-tower"}, {point.key for point in view.visible})

        view.query.set("First Whisper")
        view.apply_filter()
        self.assertEqual(["cave1"], [point.key for point in view.visible])
        view.world_var.set("other-world")
        view.apply_filter()
        self.assertEqual([], view.visible)
        self.assertIn("0 matching objects", view.status.get())
        view.query.set("")
        view.apply_filter()
        self.assertEqual(["other-unvisited"], [point.key for point in view.visible])

    def test_clear_reset_and_disabling_locations_clear_not_visited_filter(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        view.not_visited_check.invoke()
        view.location_legend["caves"].invoke()
        view.query.set("First")
        view.clear_filter()
        self.assertFalse(view.not_visited_var.get())
        self.assertEqual(set(), view._selected_location_types())
        self.assertEqual("", view.query.get())
        self.assertIn("p", {point.key for point in view.visible})

        view.not_visited_check.invoke()
        view.layer_checks["locations"].invoke()
        self.assertFalse(view.layer_vars["locations"].get())
        self.assertFalse(view.not_visited_var.get())
        self.assertIn("p", {point.key for point in view.visible})

        view.not_visited_check.invoke()
        self.assertTrue(view.layer_vars["locations"].get())
        view.reset()
        self.assertFalse(view.not_visited_var.get())

    def test_list_selection_clears_only_conflicting_location_filters(self):
        view = self.view
        extra = (
            maps.MapPoint("unknown", WORLD, "CaveUnknown", "Unknown cave", "locations",
                          (700, 100, 1), location_type="caves", in_save=None),
            maps.MapPoint("unvisited-tower", WORLD, "TowerMissing", "Missing tower", "locations",
                          (800, 100, 2), location_type="towers", in_save=False),
        )
        view.set_data(maps.MapData(self._legend_filter_points() + extra, (WORLD, "other-world")))
        view.location_legend["caves"].invoke()
        view.not_visited_check.invoke()
        self.assertEqual({"cave1", "cave2", "gyro", "tower", "unknown", "unvisited-tower"},
                         {point.key for point in view.location_rows.values()})

        def select(key):
            row = next(iid for iid, point in view.location_rows.items() if point.key == key)
            view.location_tree.selection_set(row)
            view._show_list_point()
            self.assertEqual(key, view.selected.key)
            self.assertIn(key, {point.key for point in view.visible})

        select("cave1")
        self.assertTrue(view.not_visited_var.get())
        self.assertEqual({"caves"}, view._selected_location_types())
        select("cave2")
        self.assertFalse(view.not_visited_var.get())
        self.assertEqual({"caves"}, view._selected_location_types())
        view.not_visited_check.invoke()
        select("unknown")
        self.assertFalse(view.not_visited_var.get())
        self.assertEqual({"caves"}, view._selected_location_types())
        view.not_visited_check.invoke()
        select("unvisited-tower")
        self.assertTrue(view.not_visited_var.get())
        self.assertEqual(set(), view._selected_location_types())

        view.location_legend["towers"].invoke()
        view.list_scope.set("Boats")
        view._populate_location_list()
        select("boat")
        self.assertFalse(view.not_visited_var.get())
        self.assertEqual(set(), view._selected_location_types())

    def test_site_list_stays_independent_and_selecting_a_hidden_type_clears_filter(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        view.location_legend["caves"].invoke()
        self.assertEqual({"cave1", "cave2", "gyro", "tower"},
                         {point.key for point in view.location_rows.values()})
        cave_row = next(iid for iid, point in view.location_rows.items() if point.key == "cave1")
        view.location_tree.selection_set(cave_row)
        view._show_list_point()
        self.assertEqual({"caves"}, view._selected_location_types())
        self.assertIn("cave1", {point.key for point in view.visible})

        tower_row = next(iid for iid, point in view.location_rows.items() if point.key == "tower")
        view.location_tree.selection_set(tower_row)
        view._show_list_point()
        self.assertEqual(set(), view._selected_location_types())
        self.assertEqual("tower", view.selected.key)
        self.assertIn("tower", {point.key for point in view.visible})
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)

        view.location_legend["caves"].invoke()
        view.list_scope.set("Boats")
        view._populate_location_list()
        view.location_tree.selection_set("0")
        view._show_list_point()
        self.assertEqual(set(), view._selected_location_types())
        self.assertEqual("boat", view.selected.key)
        self.assertIn("boat", {point.key for point in view.visible})

    def test_legend_controls_are_focusable_clickable_and_keyboard_activatable(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        self.root.deiconify()
        view.inspector.select(view.layers_tab)
        view.layer_canvas.yview_moveto(1)
        self.root.update()
        cave = view.location_legend["caves"]
        self.assertEqual("hand2", cave.cget("cursor"))
        self.assertTrue(int(cave.cget("takefocus")))
        self.assertTrue(cave.bind("<Return>"))
        cave.event_generate("<Enter>", x=15, y=15)
        cave.event_generate("<ButtonPress-1>", x=15, y=15)
        cave.event_generate("<ButtonRelease-1>", x=15, y=15)
        self.root.update()
        self.assertEqual({"caves"}, view._selected_location_types())
        cave.focus_force()
        self.root.update()
        cave.event_generate("<KeyPress-Return>")
        self.root.update()
        self.assertEqual(set(), view._selected_location_types())
        cave.event_generate("<KeyPress-space>")
        self.root.update()
        self.assertEqual({"caves"}, view._selected_location_types())

    def test_not_visited_control_is_focusable_clickable_and_keyboard_activatable(self):
        view = self.view
        view.set_data(maps.MapData(self._legend_filter_points(), (WORLD, "other-world")))
        self.root.deiconify()
        view.inspector.select(view.layers_tab)
        self.root.update()
        control = view.not_visited_check
        canvas_height = view.layer_canvas.bbox("all")[3]
        view.layer_canvas.yview_moveto(control.winfo_y() / canvas_height)
        self.root.update()
        self.assertEqual("hand2", str(control.cget("cursor")))
        self.assertTrue(int(control.cget("takefocus")))
        self.assertTrue(control.bind("<Return>"))
        control.event_generate("<Enter>", x=10, y=10)
        control.event_generate("<ButtonPress-1>", x=10, y=10)
        control.event_generate("<ButtonRelease-1>", x=10, y=10)
        self.root.update()
        self.assertTrue(view.not_visited_var.get())
        control.focus_force()
        self.root.update()
        control.event_generate("<KeyPress-Return>")
        self.root.update()
        self.assertFalse(view.not_visited_var.get())
        control.event_generate("<KeyPress-space>")
        self.root.update()
        self.assertTrue(view.not_visited_var.get())

    def test_individual_cluster_selection_and_single_marker_double_click_zoom_full(self):
        view = self.view
        cluster = next(group for group in view.groups.values() if len(group) == 2)
        view._select_group(cluster)
        self.assertLess(view.camera.scale, maps.MapCamera.MAX_SCALE)
        view.selection_list.selection_clear(0, tk.END)
        view.selection_list.selection_set(1)
        view._select_list()
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)
        self.assertEqual(self.points[2].position[:2], (view.camera.x, view.camera.y))
        view.draw()
        width, height = view.canvas.winfo_width(), view.canvas.winfo_height()
        view._expand(SimpleNamespace(x=width / 2, y=height / 2))
        self.assertEqual(maps.MapCamera.MAX_SCALE, view.camera.scale)
        view.draw()
        labels = [view.canvas.itemcget(item, "text") for item in view.canvas.find_all()
                  if view.canvas.type(item) == "text"]
        self.assertTrue(any("1,000." in text for text in labels))

    def test_boat_layer_only_classifies_root_boats_not_their_equipment(self):
        raw = save([], [
            actor("BP_Boat_Barge_C1031"),
            actor("BP_Boat_Barge_C1031LevelInstanceBP_Module_Engine_C123"),
            actor("BP_BoatExtension_HeliPad_C-123"),
        ])
        data = maps.read_map(raw)
        boats = [p for p in data.points if p.layer == "boats"]
        self.assertEqual(["BP_Boat_Barge_C1031"], [p.actor for p in boats])
        self.assertEqual("Save", boats[0].source)

    def test_list_remains_usable_in_short_map_panel(self):
        self.root.deiconify()
        self.root.geometry("1040x410")
        self.view.inspector.select(self.view.list_tab)
        self.root.update()
        self.assertGreaterEqual(self.view.location_tree.winfo_height(), 85)
        pending = list(self.view.list_tab.winfo_children())
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, (gui.ttk.Button, gui.ttk.Entry, gui.ttk.Combobox)):
                self.assertLessEqual(
                    widget.winfo_rooty() + widget.winfo_height(),
                    self.view.list_tab.winfo_rooty() + self.view.list_tab.winfo_height(),
                )

    def test_coordinate_warp_button_fits_at_minimum_editor_width(self):
        self.root.deiconify()
        self.root.geometry("1008x650")
        self.root.update()
        button = self.view.warp_coordinates_button
        for control in (button, self.view.warp_button, self.view.world_combo):
            with self.subTest(control=control):
                self.assertGreaterEqual(control.winfo_width(), control.winfo_reqwidth())
                self.assertLessEqual(
                    control.winfo_rootx() + control.winfo_width(),
                    self.root.winfo_rootx() + self.root.winfo_width(),
                )
        search = next(
            widget for widget in button.master.winfo_children()
            if isinstance(widget, gui.ttk.Entry) and not isinstance(widget, gui.ttk.Combobox)
        )
        self.assertGreaterEqual(search.winfo_width(), 60)

    def test_default_layers_toggle_search_clear_and_world_switch(self):
        view = self.view
        self.assertEqual(["p", "h1", "h2"], [p.key for p in view.visible])
        view.layer_vars["containers"].set(True)
        view.apply_filter()
        self.assertEqual(4, len(view.visible))
        view.query.set("teacher")
        view.apply_filter(fit=True)
        self.assertEqual(["h2"], [p.key for p in view.visible])
        view.query.set("-teacher")
        view.apply_filter()
        self.assertEqual(["p", "h1", "c"], [p.key for p in view.visible])
        view.clear_filter()
        self.assertEqual(4, len(view.visible))
        view.world_var.set("other-world")
        view.apply_filter(fit=True)
        self.assertEqual(["p2"], [p.key for p in view.visible])
        self.assertIn("Player (1)", view.layer_checks["player"]["text"])

    def test_warp_picker_uses_click_world_coordinates_and_current_player_height(self):
        view = self.view
        callback = Mock()
        view.warp_player_callback = callback
        view.apply_filter()
        self.assertNotIn("disabled", view.warp_button.state())
        view.camera = maps.MapCamera(100, -200, 2)
        view._toggle_warp()
        self.assertTrue(view._picking_warp)
        self.assertEqual("crosshair", view.canvas.cget("cursor"))
        event = SimpleNamespace(x=100, y=150)
        expected = view.camera.world(event.x, event.y,
                                     view.canvas.winfo_width(), view.canvas.winfo_height())
        with patch.object(view, "_hit", return_value=[]):
            view._press(event)
            view._release(event)
        callback.assert_called_once_with(WORLD, (*expected, 0))
        self.assertFalse(view._picking_warp)
        self.assertEqual("Warp player...", view.warp_button.cget("text"))
        self.assertEqual("", view.canvas.cget("cursor"))

    def test_warp_picker_uses_exact_marker_coordinates_including_height(self):
        view = self.view
        callback = Mock()
        view.warp_player_callback = callback
        point = maps.MapPoint(
            "target", WORLD, "pod3", "Target", "humans",
            (1000.125, -2000.875, 3.456789),
        )
        view.set_data(maps.MapData((self.points[0], point), (WORLD,)))
        self.assertIsNone(view.selected)
        view.draw()
        x, y = view.camera.screen(*point.position[:2],
                                  view.canvas.winfo_width(), view.canvas.winfo_height())
        event = SimpleNamespace(x=x + 2, y=y + 1)
        self.assertEqual([point], view._hit(event.x, event.y))
        view._toggle_warp()
        view._press(event)
        view._release(event)
        callback.assert_called_once_with(WORLD, point.position)
        view._select_group([point])
        self.assertIn("1000.125, -2000.875, 3.456789", view.details.get("1.0", tk.END))

    def test_warp_picker_uses_exact_game_only_site_coordinates(self):
        view = self.view
        callback = Mock()
        view.warp_player_callback = callback
        site = maps.MapPoint(
            "site", WORLD, "CaveLocator", "Cave", "locations",
            (-321.0123, 456.7891, -12.3456), in_save=False, location_type="caves",
        )
        view.layer_vars["locations"].set(True)
        view.set_data(maps.MapData((self.points[0], site), (WORLD,)))
        self.assertIsNone(view.selected)
        view.draw()
        x, y = view.camera.screen(*site.position[:2],
                                  view.canvas.winfo_width(), view.canvas.winfo_height())
        event = SimpleNamespace(x=x, y=y)
        self.assertEqual([site], view._hit(event.x, event.y))
        view._toggle_warp()
        view._press(event)
        view._release(event)
        callback.assert_called_once_with(WORLD, site.position)

    def test_clustered_warp_requires_a_choice_unless_an_object_is_selected(self):
        view = self.view
        callback = Mock()
        view.warp_player_callback = callback
        view.apply_filter()
        cluster = [self.points[1], self.points[2]]
        event = SimpleNamespace(x=100, y=150)
        view._toggle_warp()
        with patch.object(view, "_hit", return_value=cluster):
            view._press(event)
            view._release(event)
        callback.assert_not_called()
        self.assertFalse(view._picking_warp)
        self.assertEqual(str(view.details_tab), view.inspector.select())
        self.assertIn("Select one", view.status.get())
        view.selection_list.selection_clear(0, tk.END)
        view.selection_list.selection_set(1)
        view._select_list()
        view.warp_coordinates_button.invoke()
        callback.assert_called_once_with(WORLD, self.points[2].position)

        callback.reset_mock()
        view._toggle_warp()
        with patch.object(view, "_hit", return_value=cluster):
            view._press(event)
            view._release(event)
        callback.assert_called_once_with(WORLD, self.points[2].position)

    def test_coordinate_warp_defaults_to_selected_object_or_current_player(self):
        view = self.view
        callback = Mock()
        view.warp_player_callback = callback
        view.apply_filter()
        self.assertNotIn("disabled", view.warp_coordinates_button.state())
        view.warp_coordinates_button.invoke()
        callback.assert_called_once_with(WORLD, self.points[0].position)

        callback.reset_mock()
        view._select_group([self.points[1]])
        view._toggle_warp()
        view.warp_coordinates_button.invoke()
        self.assertFalse(view._picking_warp)
        callback.assert_called_once_with(WORLD, self.points[1].position)

        callback.reset_mock()
        view.world_var.set("other-world")
        view.apply_filter()
        view.warp_coordinates_button.invoke()
        callback.assert_called_once_with("other-world", self.points[4].position)

    def test_warp_picker_drag_cancel_filter_change_and_missing_player_do_not_warp(self):
        view = self.view
        callback = Mock()
        view.warp_player_callback = callback
        view.apply_filter()
        view._toggle_warp()
        view._press(SimpleNamespace(x=100, y=100))
        view._drag(SimpleNamespace(x=150, y=150))
        view._release(SimpleNamespace(x=150, y=150))
        callback.assert_not_called()
        self.assertTrue(view._picking_warp)
        view._toggle_warp()
        self.assertFalse(view._picking_warp)
        view._toggle_warp()
        view.world_var.set("other-world")
        view.apply_filter()
        self.assertFalse(view._picking_warp)
        view._toggle_warp()
        view.reset()
        self.assertFalse(view._picking_warp)
        self.assertIn("disabled", view.warp_button.state())
        self.assertIn("disabled", view.warp_coordinates_button.state())
        view.set_data(maps.MapData((self.points[1],), (WORLD,)))
        self.assertIn("disabled", view.warp_button.state())
        self.assertIn("disabled", view.warp_coordinates_button.state())
        view._toggle_warp()
        view.warp_coordinates_button.invoke()
        self.assertFalse(view._picking_warp)
        callback.assert_not_called()
        another_player = maps.MapPoint(
            "second-player", WORLD, "player3", "Another player", "player", (1, 2, 3),
        )
        view.set_data(maps.MapData((self.points[0], another_player), (WORLD,)))
        self.assertIn("disabled", view.warp_button.state())
        self.assertIn("disabled", view.warp_coordinates_button.state())
        view.warp_coordinates_button.invoke()
        callback.assert_not_called()

    def test_clusters_keep_each_human_selectable_and_copyable(self):
        view = self.view
        clusters = [group for group in view.groups.values() if len(group) == 2]
        self.assertEqual(1, len(clusters))
        view._select_group(clusters[0])
        self.assertEqual(2, view.selection_list.size())
        view.selection_list.selection_clear(0, tk.END)
        view.selection_list.selection_set(1)
        view._select_list()
        details = view.details.get("1.0", "end-1c")
        for text in (BOB, "Teacher", "1000.5, 2000.5, 310", "3D distance"):
            self.assertIn(text, details)
        self.assertLess(details.index("X / Y / Z"), details.index("Layer:"))
        view.copy_details()
        self.assertEqual(details, self.root.clipboard_get())
        view.query.set("Doctor")
        view.apply_filter()
        self.assertEqual(["h1"], [point.key for point in view.selected_group])

    def test_hit_selection_zoom_expansion_and_pan_are_read_only(self):
        view = self.view
        point = self.points[1]
        x, y = view.camera.screen(*point.position[:2],
                                   view.canvas.winfo_width(), view.canvas.winfo_height())
        self.assertEqual(2, len(view._hit(x, y)))
        event = SimpleNamespace(x=x, y=y)
        view._press(event)
        view._release(event)
        self.assertIsNotNone(view.selected)
        before = view.camera.scale
        view._expand(event)
        self.assertGreater(view.camera.scale, before)
        old_x = view.camera.x
        view._press(SimpleNamespace(x=100, y=100))
        view._drag(SimpleNamespace(x=160, y=100))
        view._release(SimpleNamespace(x=160, y=100))
        self.assertLess(view.camera.x, old_x)
        self.assertEqual((1000, 2000, 300), self.points[1].position)

    def test_no_match_missing_player_and_reset_are_explicit(self):
        view = self.view
        view.query.set("not-a-match")
        view.apply_filter()
        self.root.update_idletasks()
        self.assertEqual([], view.visible)
        self.assertEqual({}, view.groups)
        self.assertIn("0 matching", view.status.get())
        view.set_data(maps.MapData((self.points[1],), (WORLD,)))
        view.center_player()
        self.assertIn("No unique saved player", view.status.get())
        view.reset("Could not read map; retry.")
        self.root.update_idletasks()
        self.assertIsNone(view.data)
        self.assertEqual([], view.visible)
        self.assertIn("retry", view.status.get())

    def test_move_button_is_only_enabled_for_eligible_objects_and_calls_selected_marker(self):
        move = Mock()
        view = SaveMapView(
            self.root, reload_map=Mock(),
            matches_filter=gui.LastCaretakerEditor._matches_filter, move_selected=move,
        )
        self.addCleanup(view.destroy)
        movable = maps.MapPoint(
            "movable", WORLD, "BP_Module_Human_Pod_C1", "Human pod", "pods", (1, 2, 3),
            actor_offset=12, move_block_reason="",
        )
        fixed = maps.MapPoint(
            "fixed", WORLD, "BP_Building_C1", "Building", "other", (3, 4, 5),
            actor_offset=18,
        )
        view.set_data(maps.MapData((movable, fixed), (WORLD,)))
        view._select_group([movable])
        self.assertEqual(tk.NORMAL, str(view.move_button["state"]))
        view._move_selected()
        move.assert_called_once_with(movable)
        view._select_group([fixed])
        self.assertEqual(tk.DISABLED, str(view.move_button["state"]))
        view._move_selected()
        move.assert_called_once_with(movable)

    def test_coincident_different_layers_remain_selectable_in_one_cluster(self):
        points = (
            self.points[0],
            maps.MapPoint("h", WORLD, "pod", "Human", "humans", (0, 0, 0), ALICE),
        )
        self.view.set_data(maps.MapData(points, (WORLD,)))
        self.root.update_idletasks()
        self.assertEqual(1, len(self.view.groups))
        self.assertEqual({"p", "h"}, {p.key for p in next(iter(self.view.groups.values()))})

    def test_named_location_is_labelled_and_details_name_their_source(self):
        place = maps.MapPoint("l", WORLD, "VoyageLocatorActor_UAID_X", "Platform Omega", "locations",
                              (50, 50, 1), details=("Position source: game world data (not stored in the save)",
                                                    "In this save's map-marker list: yes"))
        self.view.set_data(maps.MapData((self.points[0], place), (WORLD,), notices=("Note A",)))
        self.root.update_idletasks()
        self.view.draw()
        texts = [self.view.canvas.itemcget(item, "text")
                 for item in self.view.canvas.find_withtag("marker-label")]
        self.assertIn("Platform Omega", texts)
        self.assertIn("Note A", self.view.status.get())
        self.view.query.set("omega")
        self.view.apply_filter()
        self.assertEqual(["l"], [point.key for point in self.view.visible])
        self.view._select_group([place])
        details = self.view.details.get("1.0", "end-1c")
        self.assertIn("game world data", details)
        self.assertIn("Layer: Named locations", details)
        self.view.layer_vars["locations"].set(False)
        self.view.apply_filter()
        self.assertEqual([], self.view.visible)

    def test_fit_ignores_far_outliers_only_among_many_objects(self):
        core = [(x * 100.0, y * 100.0, 0.0) for x in range(10) for y in range(10)]
        far = [(500.0, -100000.0, 0.0)]
        self.assertEqual(core, SaveMapView._core_positions(core + far))
        self.assertEqual(far, SaveMapView._core_positions(far))
        few = core[:5] + far
        self.assertEqual(few, SaveMapView._core_positions(few))


class MapEditorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.info = self.enterContext(patch.object(gui.messagebox, "showinfo"))
        for target, name, value in (
            (gui, "_load_settings", {}),
            (gui.LastCaretakerEditor, "_load_startup_save", None),
            (gui.LastCaretakerEditor, "_refresh_saves", None),
            (gui.LastCaretakerEditor, "_open_full_screen", None),
            (gui.LastCaretakerEditor, "_write_settings", True),
        ):
            mock = patch.object(target, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        # Collect Tk garbage on the main thread so a later collection cannot
        # run Variable.__del__ on the map worker thread and abort Tcl.
        gc.collect()
        self.addCleanup(gc.collect)
        self.app = gui.LastCaretakerEditor()
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        gc.collect()
        self.app.document = document(save(
            [human(BOB, result="Success")], [pod(BOB, position=(100, 200, 300))],
            player_position=(0, 0, 0),
        ))

    def test_map_loads_in_worker_only_when_selected_caches_and_never_writes(self):
        app = self.app
        main = threading.get_ident()
        threads = []
        original = maps.read_map

        def read(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with (patch.object(gui.map_io, "read_map", side_effect=read) as parse,
              patch.object(gui.save_io, "save_changes") as write,
              patch.object(gui.save_io, "backup_save") as backup):
            app._map_tab_changed()
            parse.assert_not_called()
            app.notebook.select(app.map_tab)
            app._map_tab_changed()
            self.assertEqual(1, parse.call_count)
            app._map_tab_changed()
            self.assertEqual(1, parse.call_count)
            app._load_map(force=True)
            self.assertEqual(2, parse.call_count)
            write.assert_not_called()
            backup.assert_not_called()
        self.assertTrue(all(thread != main for thread in threads))
        self.assertEqual(2, len([p for p in app.map_view.visible if p.layer != "locations"]))
        self.assertGreater(len([p for p in app.map_view.visible if p.layer == "locations"]), 100)
        self.assertEqual(maps.NAMED_LOCATIONS_PATH, parse.call_args.kwargs["named_locations"])
        self.assertEqual(0, gui._unsaved_edits(app))
        app._reset_map()
        self.assertIsNone(app._map_document)
        self.assertIsNone(app.map_view.data)

    def test_parser_error_is_reported_and_retry_remains_possible(self):
        app = self.app
        with (patch.object(gui.map_io, "read_map", side_effect=gvas.GvasError("bad map")),
              patch.object(gui.messagebox, "showerror") as error):
            app._load_map()
        error.assert_called_once_with("Could not read map", "bad map")
        self.assertIsNone(app._map_document)
        self.assertIn("Refresh map", app.map_view.status.get())
        app._load_map()
        self.assertIs(app.document, app._map_document)

    def test_move_selected_object_confirms_backs_up_saves_and_reloads(self):
        app = self.app
        point = maps.MapPoint(
            "pod", WORLD, "BP_Module_Human_Pod_C123", "Human pod", "pods", (2, 3, 4),
            actor_offset=500, move_block_reason="",
        )
        player = maps.MapPoint(
            "player", WORLD, "BP_FirstPersonCharacter", "Saved player position", "player",
            (100, 200, 300),
        )
        app.map_view.set_data(maps.MapData((point, player), (WORLD,)))
        app._map_document = app.document
        backup = Path("Slot.sav.test.bak")
        with (
            patch.object(gui, "_game_is_running", return_value=False),
            patch.object(gui.messagebox, "askyesno", return_value=True) as confirm,
            patch.object(gui.LastCaretakerEditor, "_save_map_relocation", return_value=backup) as save_move,
            patch.object(app, "_load", return_value=True) as reload_save,
        ):
            app._move_selected_map_object(point)
        confirm.assert_called_once()
        self.assertIn("saved player position, 1 m north", confirm.call_args.args[1])
        save_move.assert_called_once_with(
            app.document, point, (100, 199.0, 300), max(1, app.keep_var.get()),
        )
        reload_save.assert_called_once_with(app.document.path, confirm_discard=False)
        self.assertIn("backup: Slot.sav.test.bak", app.status_var.get())
        self.info.assert_called_once_with(
            "Saved", f"Saved successfully.\n\n{app.document.path}", parent=app)

    def test_move_stops_when_game_is_running_or_confirmation_is_declined(self):
        app = self.app
        point = maps.MapPoint(
            "pod", WORLD, "BP_Module_Human_Pod_C123", "Human pod", "pods", (2, 3, 4),
            actor_offset=500, move_block_reason="",
        )
        player = maps.MapPoint(
            "player", WORLD, "BP_FirstPersonCharacter", "Saved player position", "player",
            (100, 200, 300),
        )
        app.map_view.set_data(maps.MapData((point, player), (WORLD,)))
        app._map_document = app.document
        with (
            patch.object(gui, "_game_is_running", return_value=True),
            patch.object(gui.messagebox, "showerror") as error,
            patch.object(gui.messagebox, "askyesno") as confirm,
            patch.object(gui.LastCaretakerEditor, "_save_map_relocation") as save_move,
        ):
            app._move_selected_map_object(point)
            error.assert_called_once()
            self.assertEqual("Game is running", error.call_args.args[0])
            confirm.assert_not_called()
            save_move.assert_not_called()

        with (
            patch.object(gui, "_game_is_running", return_value=False),
            patch.object(gui.messagebox, "askyesno", return_value=False) as confirm,
            patch.object(gui.LastCaretakerEditor, "_save_map_relocation") as save_move,
        ):
            app._move_selected_map_object(point)
        confirm.assert_called_once()
        save_move.assert_not_called()
        self.info.assert_not_called()

    def test_move_write_failure_noop_and_reload_failure_never_report_saved(self):
        app = self.app
        point = maps.MapPoint(
            "pod", WORLD, "BP_Module_Human_Pod_C123", "Human pod", "pods", (2, 3, 4),
            actor_offset=500, move_block_reason="",
        )
        player = maps.MapPoint(
            "player", WORLD, "BP_FirstPersonCharacter", "Saved player position",
            "player", (100, 200, 300),
        )
        app.map_view.set_data(maps.MapData((point, player), (WORLD,)))
        app._map_document = app.document
        for state in ("write", "reload", "noop"):
            with (
                self.subTest(state=state),
                patch.object(gui, "_game_is_running", return_value=False),
                patch.object(gui.messagebox, "askyesno", return_value=True),
                patch.object(gui.messagebox, "showerror"),
                patch.object(app, "_save_map_relocation",
                             side_effect=OSError("write failed") if state == "write" else None,
                             return_value=None if state == "noop" else Path("move.bak")),
                patch.object(app, "_load", return_value=state != "reload"),
            ):
                app._move_selected_map_object(point)
            self.info.assert_not_called()

    def _warp_document(self):
        app = self.app
        app.document = document(warp_save())
        app.map_view.set_data(maps.read_map(app.document.raw))
        app._map_document = app.document
        return app

    def test_warp_reviews_coordinates_confirms_saves_and_reloads_without_on_open_repair(self):
        app = self._warp_document()
        player = app.map_view.data.player(WORLD)
        backup = Path("warp.sav.test.bak")
        destination = (1000.25, -2000.5, player.position[2])
        target = (-12.25, 567.5, 25.750123)
        with (
            patch.object(gui, "_game_is_running", return_value=False),
            patch.object(gui.save_io, "save_is_stale", return_value=False),
            patch.object(gui, "_PlayerWarpDialog", return_value=SimpleNamespace(result=target)) as dialog,
            patch.object(gui.messagebox, "askyesno", return_value=True) as confirm,
            patch.object(app, "_save_player_warp", return_value=backup) as write,
            patch.object(app, "_load", return_value=True) as reload_save,
            patch.object(app, "_repair_and_top_off") as repair,
        ):
            app.repair_on_open_var.set(True)
            app._warp_player_on_map(WORLD, destination)
        dialog.assert_called_once_with(app, WORLD, destination)
        confirm.assert_called_once()
        self.assertIn("-12.25, 567.5, 25.750123", confirm.call_args.args[1])
        self.assertIn("NOT checked", confirm.call_args.args[1])
        self.assertEqual("no", confirm.call_args.kwargs["default"])
        write.assert_called_once_with(app.document, player, target, app.keep_var.get())
        reload_save.assert_called_once_with(app.document.path, confirm_discard=False)
        repair.assert_not_called()
        self.assertIn("-12.25, 567.5, 25.750123", app.status_var.get())
        self.assertIn("backup: warp.sav.test.bak", app.status_var.get())
        self.info.assert_called_once()

    def test_real_coordinate_dialog_defaults_and_returns_all_edited_axes(self):
        app = self.app
        seen = []
        failures = []

        def enter_coordinates():
            dialogs = [child for child in app.winfo_children() if isinstance(child, gui._PlayerWarpDialog)]
            dialog = dialogs[0]
            try:
                seen.append(tuple(dialog.coordinate_entries[axis].get() for axis in ("X", "Y", "Z")))
                for axis, value in zip(("X", "Y", "Z"), ("-1.25", "222.5", "-12.75")):
                    dialog.coordinate_entries[axis].delete(0, tk.END)
                    dialog.coordinate_entries[axis].insert(0, value)
                dialog.ok()
            except Exception as exc:
                failures.append(exc)
                dialog.cancel()

        app.after(30, enter_coordinates)
        dialog = gui._PlayerWarpDialog(app, WORLD, (45.25, -80.5, 3.125))
        self.assertEqual([], failures)
        self.assertEqual([("45.25", "-80.5", "3.125")], seen)
        self.assertEqual((-1.25, 222.5, -12.75), dialog.result)

    def test_direct_coordinates_write_all_axes_and_create_a_backup(self):
        app = self.app
        raw = warp_save()
        original = gui.save_io.compress_container(raw)
        target = (-42.125, 1234.5, 6.75)
        seen = []
        failures = []

        def enter_coordinates():
            dialogs = [child for child in app.winfo_children() if isinstance(child, gui._PlayerWarpDialog)]
            if not dialogs:
                app.after(10, enter_coordinates)
                return
            dialog = dialogs[0]
            try:
                seen.append(tuple(dialog.coordinate_entries[axis].get() for axis in ("X", "Y", "Z")))
                for axis, value in zip(("X", "Y", "Z"), target):
                    dialog.coordinate_entries[axis].delete(0, tk.END)
                    dialog.coordinate_entries[axis].insert(0, repr(value))
                dialog.ok()
            except Exception as exc:
                failures.append(exc)
                dialog.cancel()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "direct-warp.sav"
            path.write_bytes(original)
            app.document = gui.save_io.load_save(path)
            data = maps.read_map(app.document.raw)
            app.map_view.set_data(data)
            app._map_document = app.document
            player = data.player(WORLD)
            with (
                patch.object(gui, "_game_is_running", return_value=False),
                patch.object(gui.messagebox, "askyesno", return_value=True) as confirm,
                patch.object(gui.messagebox, "showerror") as error,
                patch.object(gui.messagebox, "showwarning"),
            ):
                app.after(10, enter_coordinates)
                app.map_view.warp_coordinates_button.invoke()
            self.assertEqual([], failures)
            self.assertEqual([tuple(repr(value) for value in player.position)], seen)
            error.assert_not_called()
            confirm.assert_called_once()
            backups = gui.save_io.list_backups(path)
            self.assertEqual(1, len(backups))
            self.assertEqual(original, backups[0].read_bytes())
            updated = gui.save_io.load_save(path)
            self.assertEqual(target, maps.read_map(updated.raw).player(WORLD).position)
            self.info.assert_called_once()

    def test_canceling_coordinates_or_confirmation_never_writes(self):
        for result, accept in ((None, True), ((1000, 2000, 10), False)):
            with self.subTest(result=result, accept=accept):
                app = self._warp_document()
                with (
                    patch.object(gui, "_game_is_running", return_value=False),
                    patch.object(gui.save_io, "save_is_stale", return_value=False),
                    patch.object(gui, "_PlayerWarpDialog", return_value=SimpleNamespace(result=result)),
                    patch.object(gui.messagebox, "askyesno", return_value=accept) as confirm,
                    patch.object(app, "_save_player_warp") as write,
                ):
                    app._warp_player_on_map(WORLD, (1000, 2000, 3))
                write.assert_not_called()
                self.assertIn("canceled", app.status_var.get())
                if result is None:
                    confirm.assert_not_called()

    def test_warp_pending_edits_stale_map_stale_disk_game_and_missing_player_are_blocked(self):
        for problem in ("pending", "human", "map", "disk", "game", "player", "layout", "keep"):
            with self.subTest(problem=problem):
                app = self._warp_document()
                app.pending.clear()
                app.human_pending.clear()
                app.keep_var.set(20)
                if problem == "pending":
                    app.pending[1] = 2
                elif problem == "human":
                    app.human_pending[(WORLD, ALICE)] = DOCTOR
                elif problem == "map":
                    app._map_document = None
                elif problem == "player":
                    app.map_view.set_data(maps.MapData((), (WORLD,)))
                elif problem == "layout":
                    app.document = document(warp_save(attachment=b"\1\0\0\0"))
                    app.map_view.set_data(maps.read_map(app.document.raw))
                    app._map_document = app.document
                elif problem == "keep":
                    app.keep_var.set("bad")
                with (
                    patch.object(gui, "_game_is_running", return_value=problem == "game"),
                    patch.object(gui.save_io, "save_is_stale", return_value=problem == "disk"),
                    patch.object(gui, "_PlayerWarpDialog") as dialog,
                    patch.object(gui.messagebox, "showerror") as error,
                    patch.object(app, "_save_player_warp") as write,
                ):
                    app._warp_player_on_map(WORLD, (1000, 2000, 3))
                error.assert_called_once()
                dialog.assert_not_called()
                write.assert_not_called()
        app.pending.clear()
        app.human_pending.clear()
        app.keep_var.set(20)

    def test_warp_write_failure_noop_and_reload_failure_have_explicit_status(self):
        for state in ("write", "reload", "noop"):
            with self.subTest(state=state):
                app = self._warp_document()
                with (
                    patch.object(gui, "_game_is_running", return_value=False),
                    patch.object(gui.save_io, "save_is_stale", return_value=False),
                    patch.object(gui, "_PlayerWarpDialog", return_value=SimpleNamespace(result=(1, 2, 3))),
                    patch.object(gui.messagebox, "askyesno", return_value=True),
                    patch.object(gui.messagebox, "showerror") as error,
                    patch.object(app, "_save_player_warp",
                                 side_effect=OSError("backup denied") if state == "write" else None,
                                 return_value=None if state == "noop" else Path("warp.bak")),
                    patch.object(app, "_load", return_value=state != "reload") as reload_save,
                ):
                    app._warp_player_on_map(WORLD, (1000, 2000, 3))
                if state == "write":
                    error.assert_called_once()
                    reload_save.assert_not_called()
                    self.assertIn("failed", app.status_var.get())
                else:
                    error.assert_not_called()
                    self.assertIn("reload failed" if state == "reload" else "no save or backup",
                                  app.status_var.get())
                self.info.assert_not_called()

    def test_reload_refreshes_positions_on_the_selected_map_and_preserves_filters(self):
        app = self.app
        app.notebook.select(app.map_tab)
        app._map_tab_changed()
        app.map_view.query.set(BOB)
        app.map_view.apply_filter()
        updated = document(save(
            [human(BOB, result="Success")], [pod(BOB, position=(500, 600, 700))],
            player_position=(0, 0, 0),
        ))
        with patch.object(gui.save_io, "load_save", return_value=updated):
            self.assertTrue(app._load(updated.path, confirm_discard=False))
        self.assertIs(updated, app._map_document)
        self.assertEqual(BOB, app.map_view.query.get())
        self.assertEqual([(5, 6, 7)], [point.position for point in app.map_view.visible])

    def test_map_controls_fit_default_and_minimum_editor_window_sizes(self):
        app = self.app
        app.deiconify()
        app.notebook.select(app.map_tab)
        for geometry in ("1360x820", "1040x650"):
            app.notebook.select(app.map_tab)
            app.geometry(geometry)
            app.update()
            app.map_view.fit_visible()
            app.update_idletasks()
            width, height = app.winfo_width(), app.winfo_height()
            widgets = [app.map_view]
            while widgets:
                widget = widgets.pop()
                widgets.extend(widget.winfo_children())
                if (widget.winfo_viewable()
                        and isinstance(widget, (gui.ttk.Button, gui.ttk.Entry, gui.ttk.Combobox))):
                    x = widget.winfo_rootx() - app.winfo_rootx()
                    y = widget.winfo_rooty() - app.winfo_rooty()
                    self.assertLessEqual(x + widget.winfo_width(), width, (geometry, widget))
                    self.assertLessEqual(y + widget.winfo_height(), height, (geometry, widget))
            self.assertGreater(app.map_view.canvas.winfo_width(), 300)
            self.assertGreater(app.map_view.canvas.winfo_height(), 180)
            control = app.map_view.not_visited_check
            layer_canvas = app.map_view.layer_canvas
            layer_canvas.yview_moveto(control.winfo_y() / layer_canvas.bbox("all")[3])
            app.update_idletasks()
            self.assertGreaterEqual(control.winfo_width(), control.winfo_reqwidth())
            self.assertGreaterEqual(control.winfo_rootx(), layer_canvas.winfo_rootx())
            self.assertLessEqual(control.winfo_rootx() + control.winfo_width(),
                                 layer_canvas.winfo_rootx() + layer_canvas.winfo_width())
            self.assertGreaterEqual(control.winfo_rooty(), layer_canvas.winfo_rooty())
            self.assertLessEqual(control.winfo_rooty() + control.winfo_height(),
                                 layer_canvas.winfo_rooty() + layer_canvas.winfo_height())
            app.map_view.layer_canvas.yview_moveto(1)
            app.update_idletasks()
            last_layer = app.map_view.layer_checks["other"]
            self.assertLessEqual(
                last_layer.winfo_rooty() + last_layer.winfo_height(),
                app.map_view.layer_canvas.winfo_rooty() + app.map_view.layer_canvas.winfo_height(),
            )


class SiteListEditorLayoutTests(unittest.TestCase):
    def test_site_list_fits_actual_editor_at_minimum_and_default_sizes(self):
        with patch.object(gui, "_load_settings", return_value={}), \
                patch.object(gui.LastCaretakerEditor, "_refresh_saves"), \
                patch.object(gui.LastCaretakerEditor, "_load_startup_save"), \
                patch.object(gui.LastCaretakerEditor, "_open_full_screen"):
            app = gui.LastCaretakerEditor()
        try:
            app.notebook.select(app.map_tab)
            app.update()
            point = maps.MapPoint("site", WORLD, "Locator", "Named site",
                                  "locations", (100, 200, 3), in_save=False)
            app.map_view.set_data(maps.MapData((point,), (WORLD,)))
            view = app.map_view
            view.inspector.select(view.list_tab)
            for size in ("1040x680", "1360x820"):
                with self.subTest(size=size):
                    app.geometry(size)
                    app.update()
                    self.assertGreaterEqual(view.location_tree.winfo_height(), 85)
                    self.assertLessEqual(
                        view.location_tree.winfo_rooty() + view.location_tree.winfo_height(),
                        view.list_tab.winfo_rooty() + view.list_tab.winfo_height(),
                    )
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
