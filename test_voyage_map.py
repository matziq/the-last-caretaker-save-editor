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
        self.assertEqual((), data.notices)
        self.assertEqual(2, len([p for p in data.points if p.layer != "locations"]))
        with patch.object(maps, "_saved_locators", return_value={self.ACTOR}):
            place = next(p for p in maps.read_map(raw, named_locations=path).points
                         if p.layer == "locations")
        self.assertIn("In this save's map-marker list: yes", place.details)
        with patch.object(maps, "_saved_locators", return_value=None):
            place = next(p for p in maps.read_map(raw, named_locations=path).points
                         if p.layer == "locations")
        self.assertIn("unknown", " ".join(place.details))

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
        for text in (BOB, "Teacher", "1,000.5, 2,000.5, 310.0", "3D distance"):
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
            app.notebook.select(app.settings_tab)
            app.settings_canvas.yview_moveto(1)
            app.update_idletasks()
            app.map_view.layer_canvas.yview_moveto(1)
            app.update_idletasks()
            last_layer = app.map_view.layer_checks["other"]
            self.assertLessEqual(
                last_layer.winfo_rooty() + last_layer.winfo_height(),
                app.map_view.layer_canvas.winfo_rooty() + app.map_view.layer_canvas.winfo_height(),
            )


if __name__ == "__main__":
    unittest.main()
