"""Focused human parsing, structural-save, and native GUI coverage."""
from __future__ import annotations

import struct
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import last_caretaker_save_edit as editor
import last_caretaker_save_gui as gui
import voyage_gvas as gvas
import voyage_humans as humans


END = editor._pack_fstring("None")
HEADER = (
    b"GVAS" + struct.pack("<iiiHHHI", 3, 0, 0, 5, 4, 0, 0)
    + editor._pack_fstring("test") + struct.pack("<ii", 3, 0)
    + editor._pack_fstring("/Script/Voyage.TestSave")
)
WORLD = "voyageworld2"
ALICE, BOB, CAROL = "A" * 32, "B" * 32, "C" * 32
DOCTOR = humans.PROFESSION_ASSETS["Doctor"]
CHANCELLOR = humans.PROFESSION_ASSETS["Existential Chancellor"]
EXPLORER = humans.PROFESSION_ASSETS["Frontier Explorer"]
MUTATION = "/Game/Data/Assets/HumanGrow/Traits/Mutations/DA_Mutation_Test.DA_Mutation_Test"
FUTURE_JOB = humans.PROFESSION_ROOT + "Future/DA_Profession_Future.DA_Profession_Future"
ATTRIBUTE = "/Game/Data/Assets/HumanGrow/Properties/DA_Human_Property_Height.DA_Human_Property_Height"
STRENGTH = "/Game/Data/Assets/HumanGrow/Properties/DA_Human_Property_Strength.DA_Human_Property_Strength"


def tag(name, kind, payload, *children, flags=0, guid=b""):
    data = editor._pack_tag(name, editor._pack_type(kind, *children), payload)
    flag_offset = len(data) - len(payload) - 1
    return data[:flag_offset] + bytes((flags,)) + guid + payload


def string(name, value, kind="StrProperty"):
    return tag(name, kind, editor._pack_fstring(value))


def struct_map(name, entries, key_type="StrProperty"):
    return tag(
        name, "MapProperty", struct.pack("<ii", 0, len(entries)) + b"".join(entries),
        editor._pack_type(key_type), editor._pack_type("StructProperty", editor._pack_type("TestStruct")),
    )


def object_values(name, values):
    return tag(
        name, "MapProperty", struct.pack("<ii", 0, len(values))
        + b"".join(editor._pack_fstring(key) + struct.pack("<d", value) for key, value in values.items()),
        editor._pack_type("ObjectProperty"), editor._pack_type("DoubleProperty"),
    )


def trait_set(name, values, *, wide=False, guid=False):
    def encode(value):
        if wide and not humans.is_profession(value):
            data = value.encode("utf-16-le") + b"\0\0"
            return struct.pack("<i", -(len(data) // 2)) + data
        return editor._pack_fstring(value)

    return tag(
        name, "SetProperty", struct.pack("<ii", 0, len(values)) + b"".join(map(encode, values)),
        editor._pack_type("ObjectProperty"), flags=2 if guid else 0, guid=b"G" * 16 if guid else b"",
    )


def human(social_id, traits=(), *, result="Delivered", wide=False, guid=False,
          attributes=None, omit_attributes=False):
    return (
        string("Type", "EVoyageResultType::" + result, "EnumProperty")
        + string("UniqueID", social_id)
        + tag("Timestamp", "Int64Property", struct.pack("<q", 123456789))
        + tag("DeliveryTimestamp", "Int64Property", struct.pack("<q", 987654321))
        + trait_set("Traits", traits, wide=wide, guid=guid)
        + (b"" if omit_attributes else object_values(
            "properties", {ATTRIBUTE: 123.75} if attributes is None else attributes))
        + string("UnrelatedHumanField", "Keep this verbatim")
        + END
    )


def pod(social_id, traits=(), *, simulated=None, actor="BP_Module_Human_Pod_C123",
        occupied=True, prefix=b"\0", trailer=b"\0\0\0\0", wide=False,
        current_attributes=None, simulated_attributes=None, omit_current=False,
        position=None):
    props = (
        tag("bHasSeedCurrent", "BoolProperty", b"", flags=0x10)
        + tag("bHasHumanCurrent", "BoolProperty", b"", flags=0x10 if occupied else 0)
        + string("HumanId", social_id)
        + trait_set("TraitsCurrent", traits, wide=wide)
        + trait_set("TraitsSimulated", traits if simulated is None else simulated, wide=wide)
        + (b"" if omit_current else object_values(
            "HumanPropertiesCurrent", {ATTRIBUTE: 901.25} if current_attributes is None else current_attributes))
        + object_values(
            "HumanPropertiesSimulated", {ATTRIBUTE: 1000.5} if simulated_attributes is None else simulated_attributes)
        + tag("GrowTimer", "FloatProperty", struct.pack("<f", -0.25))
        + tag("bCanBeDamaged", "BoolProperty", b"", flags=0x10)
        + END
    )
    blob = prefix + props + trailer
    return (
        string("ActorName", actor, "NameProperty")
        + string("ActorClass", "/Game/Modules/BP_Module_Human_Pod.BP_Module_Human_Pod_C", "ObjectProperty")
        + (tag(
            "Transform", "StructProperty", struct.pack("<3d3f", *position, 0.0, 0.0, 0.0),
            editor._pack_type(
                "VoyagePackedTransform", editor._pack_type("/Script/Voyage")),
        ) if position is not None else b"")
        + tag("ByteData", "ArrayProperty", struct.pack("<i", len(blob)) + blob, editor._pack_type("ByteProperty"))
        + tag("Version", "UInt16Property", struct.pack("<H", 7))
        + string("UnrelatedActorField", "Keep the entire actor")
        + END
    )


def player_actor(position):
    return (
        string("ActorName", "BP_FirstPersonCharacter_New_C1", "NameProperty")
        + tag(
            "Transform", "StructProperty", struct.pack("<3d3f", *position, 0.0, 0.0, 0.0),
            editor._pack_type(
                "VoyagePackedTransform", editor._pack_type("/Script/Voyage")),
        )
        + END
    )


def struct_array(name, records, *, headered=False):
    payload = b"".join(records)
    if headered and records:
        payload = tag(name, "StructProperty", payload, editor._pack_type("TestStruct"))
    return tag(
        name, "ArrayProperty", struct.pack("<i", len(records)) + payload,
        editor._pack_type("StructProperty", editor._pack_type("TestStruct")),
    )


def world(records=(), pods=(), *, headered=False, legacy_actors=False,
          include_roster=True, player_position=None):
    cells = []
    actor_groups = [pods[index:index + 2] for index in range(0, len(pods), 2)]
    if player_position is not None:
        actor_groups.append([player_actor(player_position)])
    for cell_index, actors in enumerate(actor_groups):
        if legacy_actors:
            actor_map = struct_array("actors", actors, headered=True)
        else:
            actor_map = struct_map(
                "actors", [struct.pack("<Q", i + cell_index * 2 + 123) + actor
                           for i, actor in enumerate(actors)],
                "UInt64Property",
            )
        cells.append(editor._pack_fstring(f"cell{cell_index * 2}") + actor_map + END)
    return (
        struct_map("PersistentActors", cells, "NameProperty")
        + (struct_array("HumanGrow", records, headered=headered) if include_roster else b"")
        + struct_map("CommitteeData", [
            editor._pack_fstring("FIELD_CONT")
            + string("State", "EVoyageCommitteeState::Active", "EnumProperty")
            + tag("Flags", "IntProperty", struct.pack("<i", 17)) + END,
        ], "NameProperty")
        + string("UnrelatedWorldField", "Leave committees, progress and equipment alone")
        + END
    )


def save(records=(), pods=(), *, extra_worlds=(), **kwargs):
    entries = [editor._pack_fstring(WORLD) + world(records, pods, **kwargs)]
    entries.extend(editor._pack_fstring(name) + data for name, data in extra_worlds)
    payload = HEADER + string("BeforeProgress", "preserve") + struct_map("GameProgress", entries)
    payload += tag("AfterProgress", "BoolProperty", b"", flags=0x10) + END + b"\0" * 4
    return struct.pack("<I", len(payload)) + payload


def document(raw, path=Path("Slot.sav")):
    return editor.SaveDocument(
        path, raw, [], [], [], editor.DEFAULT_CHUNK_SIZE, 0, human_data=humans.read_humans(raw),
    )


class HumanReaderTests(unittest.TestCase):
    def test_reads_roster_pods_raw_attributes_and_timestamps(self):
        raw = save(
            [human(ALICE, (EXPLORER,)), human(BOB, (CHANCELLOR, MUTATION), result="Success")],
            [pod(BOB, (CHANCELLOR, MUTATION))],
        )
        data = humans.read_humans(raw)
        self.assertEqual([WORLD], data.worlds)
        alice, bob = data.humans
        self.assertEqual((WORLD, ALICE), alice.key)
        self.assertEqual(["In space", "In pod"], [h.location for h in data.humans])
        self.assertEqual((MUTATION,), bob.other_traits)
        self.assertEqual(123456789, bob.timestamp)
        self.assertEqual(987654321, bob.delivery_timestamp)
        self.assertEqual({ATTRIBUTE: 123.75}, bob.attributes)
        self.assertEqual({ATTRIBUTE: 901.25}, bob.pod.attributes_current)
        self.assertEqual({ATTRIBUTE: 1000.5}, bob.pod.attributes_simulated)
        self.assertTrue(all(not h.edit_error for h in data.humans))
        self.assertEqual(
            ["GameProgress", "PersistentActors", "actors", "ByteData", "TraitsCurrent"],
            [tag.name for tag in bob.pod.current.chain],
        )

    def test_reads_pod_coordinates_and_three_dimensional_distance_from_player(self):
        raw = save(
            [human(BOB, result="Success")],
            [pod(BOB, position=(1000.0, 2000.0, 3000.0))],
            player_position=(1000.0, 2000.0, 0.0),
        )
        entry = humans.read_humans(raw).humans[0]
        self.assertEqual((1000.0, 2000.0, 3000.0), entry.pod.position)
        self.assertEqual((1000.0, 2000.0, 0.0), entry.player_position)
        self.assertEqual(30.0, entry.pod_distance_m)

    def test_pod_coordinates_remain_available_without_a_player_transform(self):
        raw = save([human(BOB, result="Success")],
                   [pod(BOB, position=(1000.0, 2000.0, 3000.0))])
        entry = humans.read_humans(raw).humans[0]
        self.assertEqual((1000.0, 2000.0, 3000.0), entry.pod.position)
        self.assertIsNone(entry.pod_distance_m)

    def test_no_roster_and_empty_roster_are_supported(self):
        for raw in (save(include_roster=False), save()):
            self.assertEqual([], humans.read_humans(raw).humans)

    def test_unknown_result_is_not_mislabeled_in_space(self):
        entry = humans.read_humans(save([human(ALICE, (DOCTOR,), result="Failed")])).humans[0]
        self.assertEqual("Failed", entry.location)
        self.assertFalse(entry.in_space)
        self.assertIn("not supported", entry.edit_error)

    def test_missing_and_duplicate_pods_disable_only_the_affected_human(self):
        for pods in ([], [pod(BOB), pod(BOB, actor="BP_Module_Human_Pod_C456")]):
            with self.subTest(pods=len(pods)):
                raw = save([human(ALICE), human(BOB, result="Success")], pods)
                data = humans.read_humans(raw)
                self.assertFalse(data.humans[0].edit_error)
                self.assertTrue(data.humans[1].edit_error)
                with self.assertRaises(gvas.GvasError):
                    humans.replace_professions(raw, [(WORLD, BOB)], DOCTOR)

    def test_duplicate_roster_identity_is_rejected(self):
        with self.assertRaisesRegex(gvas.GvasError, "duplicate human"):
            humans.read_humans(save([human(ALICE), human(ALICE)]))

    def test_an_unlocated_matching_human_id_blocks_writes(self):
        raw = save([human(ALICE)], [pod(ALICE, actor="BP_Unsupported_Pod_C123")])
        self.assertIn("outside a supported pod", humans.read_humans(raw).humans[0].edit_error)
        with self.assertRaises(gvas.GvasError):
            humans.replace_professions(raw, [(WORLD, ALICE)], DOCTOR)

    def test_inactive_pod_does_not_become_a_grown_human(self):
        raw = save([human(ALICE)], [pod(ALICE, occupied=False)])
        entry = humans.read_humans(raw).humans[0]
        self.assertIsNone(entry.pod)
        self.assertFalse(entry.edit_error)
        changed, count = humans.replace_professions(raw, [entry.key], DOCTOR)
        self.assertEqual(1, count)
        self.assertEqual(save([human(ALICE, (DOCTOR,))], [pod(ALICE, occupied=False)]), changed)

    def test_direct_and_older_headered_struct_arrays_are_supported(self):
        for headered, legacy_actors in ((False, False), (True, True)):
            for prefix, trailer in ((b"", b""), (b"\0", b"\0" * 4)):
                with self.subTest(headered=headered, prefix=prefix):
                    raw = save([human(BOB, result="Success")], [pod(BOB, prefix=prefix, trailer=trailer)],
                               headered=headered, legacy_actors=legacy_actors)
                    result, count = humans.replace_professions(raw, [(WORLD, BOB)], DOCTOR)
                    self.assertEqual(1, count)
                    expected = save([human(BOB, (DOCTOR,), result="Success")],
                                    [pod(BOB, (DOCTOR,), prefix=prefix, trailer=trailer)],
                                    headered=headered, legacy_actors=legacy_actors)
                    self.assertEqual(expected, result)

    def test_trait_counts_removals_duplicates_and_bounds_are_rejected(self):
        raw = save([human(ALICE, (DOCTOR,))])
        site = humans.read_humans(raw).humans[0].traits
        for offset, value in ((site.tag.payload, 1), (site.tag.payload + 4, -1),
                              (site.tag.payload + 4, 0), (site.tag.payload + 4, 50000)):
            with self.subTest(offset=offset, value=value):
                broken = bytearray(raw)
                struct.pack_into("<i", broken, offset, value)
                with self.assertRaises(gvas.GvasError):
                    humans.read_humans(bytes(broken))
        with self.assertRaisesRegex(gvas.GvasError, "duplicate trait"):
            humans.read_humans(save([human(ALICE, (DOCTOR, DOCTOR))]))

    def test_pod_byte_counts_must_match(self):
        raw = save([human(BOB, result="Success")], [pod(BOB)])
        blob = humans.read_humans(raw).humans[0].pod.current.chain[-2]
        broken = bytearray(raw)
        struct.pack_into("<i", broken, blob.payload, blob.size - 5)
        with self.assertRaisesRegex(gvas.GvasError, "ByteData"):
            humans.replace_professions(bytes(broken), [(WORLD, BOB)], DOCTOR)

    def test_array_count_must_consume_every_record(self):
        raw = save([human(ALICE)])
        array = humans.read_humans(raw).humans[0].traits.chain[-2]
        for count in (-1, 0, 2):
            with self.subTest(count=count):
                broken = bytearray(raw)
                struct.pack_into("<i", broken, array.payload, count)
                with self.assertRaises(gvas.GvasError):
                    humans.read_humans(bytes(broken))

    def test_unknown_professions_are_visible_not_discarded(self):
        entry = humans.read_humans(save([human(ALICE, (FUTURE_JOB, MUTATION))])).humans[0]
        self.assertEqual((FUTURE_JOB,), entry.professions)
        self.assertIn("Unknown profession", humans.profession_label(FUTURE_JOB))
        self.assertEqual((MUTATION,), entry.other_traits)

    def test_invalid_envelope_and_unterminated_trait_strings_are_rejected(self):
        raw = save([human(ALICE, (DOCTOR,))])
        trait = humans.read_humans(raw).humans[0].traits.tag
        for offset in (0, trait.end - 1):
            with self.subTest(offset=offset):
                broken = bytearray(raw)
                broken[offset] ^= 0x01
                with self.assertRaises(gvas.GvasError):
                    humans.replace_professions(bytes(broken), [(WORLD, ALICE)], EXPLORER)

    def test_duplicate_world_keys_are_rejected(self):
        raw = save([human(ALICE)], extra_worlds=[(WORLD, world([human(BOB)]))])
        with self.assertRaisesRegex(gvas.GvasError, "duplicate GameProgress world"):
            humans.read_humans(raw)

    def test_real_attribute_asset_uses_a_readable_label(self):
        asset = "/Game/Test/DA_Physical_Human_Property_Life_Expectancy.DA_Physical_Human_Property_Life_Expectancy"
        self.assertEqual("Life Expectancy", humans.asset_label(asset))


class HumanProfessionTests(unittest.TestCase):
    def test_add_replace_remove_and_preserve_every_unrelated_byte(self):
        for old, new in (((), DOCTOR), ((CHANCELLOR,), DOCTOR), ((DOCTOR,), CHANCELLOR),
                         ((DOCTOR, CHANCELLOR), DOCTOR), ((DOCTOR,), None), ((FUTURE_JOB,), DOCTOR)):
            with self.subTest(old=old, new=new):
                raw = save([human(ALICE, old + (MUTATION,)), human(CAROL, (EXPLORER,))])
                rewritten, count = humans.replace_professions(raw, [(WORLD, ALICE)], new)
                expected_traits = (MUTATION,) + ((new,) if new else ())
                self.assertEqual(1, count)
                self.assertEqual(
                    save([human(ALICE, expected_traits), human(CAROL, (EXPLORER,))]), rewritten,
                )
                editor.validate_raw_save(rewritten)

    def test_batch_updates_shared_ancestors_and_all_three_pod_sets(self):
        ids = (ALICE, BOB, CAROL)
        raw = save(
            [human(social_id, (CHANCELLOR, MUTATION), result="Success") for social_id in ids],
            [pod(social_id, (CHANCELLOR, MUTATION), simulated=(EXPLORER, MUTATION),
                 actor=f"BP_Module_Human_Pod_C{index}") for index, social_id in enumerate(ids)],
        )
        result, count = humans.replace_professions(raw, [(WORLD, social_id) for social_id in ids], DOCTOR)
        expected = save(
            [human(social_id, (MUTATION, DOCTOR), result="Success") for social_id in ids],
            [pod(social_id, (MUTATION, DOCTOR), actor=f"BP_Module_Human_Pod_C{index}")
             for index, social_id in enumerate(ids)],
        )
        self.assertEqual(3, count)
        self.assertEqual(expected, result)

    def test_pod_mismatch_is_repaired_even_when_roster_already_matches(self):
        raw = save([human(BOB, (DOCTOR,), result="Success")], [pod(BOB, (EXPLORER,))])
        result, count = humans.replace_professions(raw, [(WORLD, BOB)], DOCTOR)
        self.assertEqual(1, count)
        self.assertEqual(save([human(BOB, (DOCTOR,), result="Success")], [pod(BOB, (DOCTOR,))]), result)

    def test_untouched_utf16_traits_and_guid_flags_are_byte_identical(self):
        mutation = MUTATION + "_caf\u00e9"
        raw = save([human(ALICE, (EXPLORER, mutation), wide=True, guid=True)])
        result, count = humans.replace_professions(raw, [(WORLD, ALICE)], DOCTOR)
        self.assertEqual(1, count)
        self.assertEqual(save([human(ALICE, (mutation, DOCTOR), wide=True, guid=True)]), result)

    def test_same_ids_in_different_worlds_are_not_conflated(self):
        second = ("another_world", world([human(ALICE, (EXPLORER,))]))
        raw = save([human(ALICE)], extra_worlds=[second])
        result, count = humans.replace_professions(raw, [(WORLD, ALICE)], DOCTOR)
        self.assertEqual(1, count)
        self.assertEqual(save([human(ALICE, (DOCTOR,))], extra_worlds=[second]), result)

    def test_no_op_preserves_byte_order_and_deduplicates_selection(self):
        raw = save([human(ALICE, (DOCTOR, MUTATION))])
        for keys in ([], [(WORLD, ALICE)], [(WORLD, ALICE), (WORLD, ALICE)]):
            result, count = humans.replace_professions(raw, keys, DOCTOR)
            self.assertIs(raw, result)
            self.assertEqual(0, count)

    def test_invalid_choice_or_missing_identity_never_produces_a_replacement(self):
        raw = save([human(ALICE)])
        for keys, choice in (([(WORLD, ALICE)], FUTURE_JOB), ([(WORLD, BOB)], DOCTOR)):
            with self.subTest(keys=keys, choice=choice):
                with self.assertRaises(ValueError):
                    humans.replace_professions(raw, keys, choice)

    def test_a_broken_record_in_a_batch_prevents_all_edits(self):
        raw = save([human(ALICE), human(BOB, result="Success")])
        with self.assertRaises(gvas.GvasError):
            humans.replace_professions(raw, [(WORLD, ALICE), (WORLD, BOB)], DOCTOR)

    def test_one_batch_can_give_each_human_a_different_profession(self):
        raw = save(
            [human(ALICE, (EXPLORER, MUTATION)), human(BOB, (CHANCELLOR,), result="Success"),
             human(CAROL, (DOCTOR,))],
            [pod(BOB, (CHANCELLOR,), simulated=(EXPLORER,))],
        )
        changes = {(WORLD, ALICE): DOCTOR, (WORLD, BOB): EXPLORER, (WORLD, CAROL): None}
        result, count = humans.apply_professions(raw, changes)
        expected = save(
            [human(ALICE, (MUTATION, DOCTOR)), human(BOB, (EXPLORER,), result="Success"), human(CAROL)],
            [pod(BOB, (EXPLORER,))],
        )
        self.assertEqual(3, count)
        self.assertEqual(expected, result)
        editor.validate_raw_save(result)

    def test_batch_mapping_is_validated_and_empty_or_matching_batches_are_no_ops(self):
        raw = save([human(ALICE, (DOCTOR,)), human(BOB)])
        self.assertIs(raw, humans.apply_professions(raw, {})[0])
        self.assertEqual((raw, 0), humans.apply_professions(raw, {(WORLD, ALICE): DOCTOR, (WORLD, BOB): None}))
        for changes in ({(WORLD, ALICE): DOCTOR, (WORLD, BOB): FUTURE_JOB}, {(WORLD, CAROL): DOCTOR}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    humans.apply_professions(raw, changes)

    def test_profession_differs_checks_roster_and_every_pod_set(self):
        data = humans.read_humans(save(
            [human(ALICE, (DOCTOR, MUTATION)), human(BOB, (DOCTOR,), result="Success")],
            [pod(BOB, (DOCTOR,), simulated=(EXPLORER,))],
        ))
        alice, bob = data.humans
        self.assertFalse(humans.profession_differs(alice, DOCTOR))
        self.assertTrue(humans.profession_differs(alice, None))
        self.assertTrue(humans.profession_differs(alice, EXPLORER))
        self.assertTrue(humans.profession_differs(bob, DOCTOR))


class CommitteeCoverageTests(unittest.TestCase):
    def test_catalogue_has_41_professions_and_40_distinct_committee_roles(self):
        self.assertEqual(41, len(humans.PROFESSIONS))
        self.assertEqual(41, len(humans.PROFESSION_ASSETS))
        self.assertEqual(10, len(humans.COMMITTEES))
        roles = [label for committee in humans.COMMITTEES for label in committee.professions]
        self.assertEqual(40, len(roles))
        self.assertEqual(40, len(set(roles)))
        self.assertTrue(all(label in humans.PROFESSION_ASSETS for label in roles))
        self.assertNotIn("Star Child", roles)

    def test_only_delivered_humans_count_and_duplicates_do_not_fill_extra_roles(self):
        data = humans.read_humans(save(
            [human(ALICE, (DOCTOR,)), human(CAROL, (DOCTOR,)),
             human(BOB, (CHANCELLOR,), result="Success")], [pod(BOB, (CHANCELLOR,))],
        ))
        roles = humans.committee_coverage(data)
        self.assertEqual(40, len(roles))
        matched = [role for role in roles if role.social_ids]
        self.assertEqual(1, len(matched))
        self.assertEqual("Doctor", matched[0].profession)
        self.assertEqual((ALICE, CAROL), matched[0].social_ids)
        self.assertEqual("EVoyageCommitteeState::Active", matched[0].saved_state)

    def test_coverage_changes_after_a_profession_edit_not_committee_state(self):
        raw = save([human(ALICE)])
        result, _ = humans.replace_professions(raw, [(WORLD, ALICE)], EXPLORER)
        roles = humans.committee_coverage(humans.read_humans(result))
        self.assertEqual(["Frontier Explorer"], [r.profession for r in roles if r.social_ids])
        self.assertEqual(humans.read_humans(raw).committee_states, humans.read_humans(result).committee_states)

    def test_coverage_is_scoped_to_each_world(self):
        data = humans.read_humans(save(
            [human(ALICE, (DOCTOR,))], extra_worlds=[("other", world([human(BOB)]))],
        ))
        self.assertEqual(80, len(humans.committee_coverage(data)))
        self.assertEqual([WORLD], [r.world for r in humans.committee_coverage(data) if r.social_ids])

    def test_queued_overrides_preview_coverage_for_humans_in_space_only(self):
        data = humans.read_humans(save(
            [human(ALICE, (DOCTOR,)), human(CAROL), human(BOB, (CHANCELLOR,), result="Success")],
            [pod(BOB, (CHANCELLOR,))],
        ))
        overrides = {(WORLD, ALICE): None, (WORLD, CAROL): EXPLORER, (WORLD, BOB): DOCTOR}
        roles = humans.committee_coverage(data, overrides)
        self.assertEqual([("Frontier Explorer", (CAROL,))], [(r.profession, r.social_ids) for r in roles if r.social_ids])
        self.assertEqual((DOCTOR,), data.humans[0].professions)
        self.assertEqual(["Doctor"], [r.profession for r in humans.committee_coverage(data) if r.social_ids])


class HumanAttributeTests(unittest.TestCase):
    def test_offsets_point_to_the_exact_roster_and_pod_doubles(self):
        raw = save([human(BOB, result="Success")], [pod(BOB)])
        entry = humans.read_humans(raw).humans[0]
        self.assertEqual(set(humans.ATTRIBUTE_SOURCES), entry.attribute_sets.keys())
        offsets = []
        for site in entry.attribute_sets.values():
            offset = site.offsets[ATTRIBUTE]
            offsets.append(offset)
            self.assertGreaterEqual(offset, site.tag.payload)
            self.assertLessEqual(offset + 8, site.tag.end)
            self.assertEqual(site.values[ATTRIBUTE], struct.unpack_from("<d", raw, offset)[0])
        self.assertEqual(3, len(set(offsets)))

    def test_raw_stat_fields_are_independent_and_every_other_byte_is_preserved(self):
        raw = save(
            [human(ALICE, (EXPLORER, MUTATION), attributes={ATTRIBUTE: 123.75, STRENGTH: 55.0}),
             human(BOB, (DOCTOR,), result="Success"), human(CAROL)],
            [pod(BOB, (DOCTOR,))],
        )
        changes = {
            ((WORLD, ALICE), "roster", STRENGTH): "321.25",
            ((WORLD, BOB), "current", ATTRIBUTE): -25.5,
            ((WORLD, BOB), "simulated", ATTRIBUTE): 9876.125,
        }
        result, count = humans.apply_attributes(raw, changes)
        expected = save(
            [human(ALICE, (EXPLORER, MUTATION), attributes={ATTRIBUTE: 123.75, STRENGTH: 321.25}),
             human(BOB, (DOCTOR,), result="Success"), human(CAROL)],
            [pod(BOB, (DOCTOR,), current_attributes={ATTRIBUTE: -25.5},
                 simulated_attributes={ATTRIBUTE: 9876.125})],
        )
        self.assertEqual(3, count)
        self.assertEqual(expected, result)
        self.assertEqual(len(raw), len(result))
        editor.validate_raw_save(result)

    def test_each_source_can_be_changed_without_copying_to_the_other_sources(self):
        raw = save([human(BOB, result="Success")], [pod(BOB)])
        for source in humans.ATTRIBUTE_SOURCES:
            with self.subTest(source=source):
                result, count = humans.apply_attributes(raw, {((WORLD, BOB), source, ATTRIBUTE): 17.5})
                self.assertEqual(1, count)
                before = humans.read_humans(raw).humans[0].attribute_sets
                after = humans.read_humans(result).humans[0].attribute_sets
                for name in before:
                    expected = {ATTRIBUTE: 17.5} if name == source else before[name].values
                    self.assertEqual(expected, after[name].values)

    def test_empty_and_unchanged_edits_preserve_the_original_bytes(self):
        raw = save([human(ALICE)])
        for changes in ({}, {((WORLD, ALICE), "roster", ATTRIBUTE): 123.75}):
            result, count = humans.apply_attributes(raw, changes)
            self.assertIs(raw, result)
            self.assertEqual(0, count)

    def test_same_social_id_in_different_worlds_is_not_conflated(self):
        raw = save([human(ALICE)], extra_worlds=[("other", world([human(ALICE)]))])
        result, count = humans.apply_attributes(raw, {((WORLD, ALICE), "roster", ATTRIBUTE): 42.0})
        self.assertEqual(1, count)
        self.assertEqual(
            save([human(ALICE, attributes={ATTRIBUTE: 42.0})], extra_worlds=[("other", world([human(ALICE)]))]),
            result,
        )

    def test_missing_humans_sources_and_stats_are_rejected_not_added(self):
        raw = save([human(ALICE), human(BOB, result="Success", omit_attributes=True)],
                   [pod(BOB, omit_current=True, simulated_attributes={})])
        invalid_keys = (
            ((WORLD, CAROL), "roster", ATTRIBUTE),
            ((WORLD, ALICE), "current", ATTRIBUTE),
            ((WORLD, ALICE), "invalid", ATTRIBUTE),
            ((WORLD, ALICE), "roster", STRENGTH),
            ((WORLD, BOB), "roster", ATTRIBUTE),
            ((WORLD, BOB), "current", ATTRIBUTE),
            ((WORLD, BOB), "simulated", ATTRIBUTE),
        )
        for key in invalid_keys:
            with self.subTest(key=key), self.assertRaises(ValueError):
                humans.apply_attributes(raw, {((WORLD, ALICE), "roster", ATTRIBUTE): 99, key: 55})

    def test_read_only_humans_prevent_the_whole_batch(self):
        for records, pods in (
            ([human(ALICE), human(BOB, result="Failed")], []),
            ([human(ALICE), human(BOB, result="Success")], []),
            ([human(ALICE), human(BOB, result="Success")],
             [pod(BOB), pod(BOB, actor="BP_Module_Human_Pod_C456")]),
            ([human(ALICE), human(BOB)], [pod(BOB, actor="BP_Unknown_Pod")]),
        ):
            raw = save(records, pods)
            with self.subTest(pods=len(pods)), self.assertRaises(gvas.GvasError):
                humans.apply_attributes(raw, {
                    ((WORLD, ALICE), "roster", ATTRIBUTE): 50,
                    ((WORLD, BOB), "roster", ATTRIBUTE): 60,
                })

    def test_non_numbers_and_non_finite_values_are_rejected(self):
        raw = save([human(ALICE)])
        for value in ("", "ten", None, True, "NaN", float("nan"), float("inf"), "-Infinity", "1e309"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite"):
                humans.apply_attributes(raw, {((WORLD, ALICE), "roster", ATTRIBUTE): value})
        for value, expected in ((" -12.5 ", -12.5), ("1.25e4", 12500.0), (0, 0.0)):
            self.assertEqual(expected, humans.parse_attribute_value(value))

    def test_malformed_attribute_maps_fail_closed(self):
        key = editor._pack_fstring(ATTRIBUTE)
        entry = key + struct.pack("<d", 1.0)
        malformed = (
            (struct.pack("<ii", 1, 1) + entry, "DoubleProperty"),
            (struct.pack("<ii", 0, 2) + entry * 2, "DoubleProperty"),
            (struct.pack("<ii", 0, 0) + entry, "DoubleProperty"),
            (struct.pack("<ii", 0, 1) + key[:-1] + b"x" + struct.pack("<d", 1.0), "DoubleProperty"),
            (struct.pack("<ii", 0, 1) + editor._pack_fstring("") + struct.pack("<d", 1.0), "DoubleProperty"),
            (struct.pack("<ii", 0, 1) + entry, "FloatProperty"),
        )
        for payload, kind in malformed:
            raw = tag("properties", "MapProperty", payload,
                      editor._pack_type("ObjectProperty"), editor._pack_type(kind))
            prop, _ = gvas.read_tag(raw, 0)
            with self.subTest(kind=kind, payload=payload[:8]), self.assertRaises(gvas.GvasError):
                humans._attributes(raw, prop)

    def test_legacy_arrays_and_pod_envelopes_support_stat_updates(self):
        for headered, prefix, trailer in ((False, b"\0", b"\0" * 4), (True, b"", b"")):
            with self.subTest(headered=headered):
                raw = save([human(BOB, result="Success")], [pod(BOB, prefix=prefix, trailer=trailer)],
                           headered=headered, legacy_actors=headered)
                result, count = humans.apply_attributes(raw, {((WORLD, BOB), "current", ATTRIBUTE): 15})
                expected = save(
                    [human(BOB, result="Success")],
                    [pod(BOB, prefix=prefix, trailer=trailer, current_attributes={ATTRIBUTE: 15})],
                    headered=headered, legacy_actors=headered,
                )
                self.assertEqual((expected, 1), (result, count))


class HumanSaveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "Slot.sav"
        self.raw = save([human(ALICE)])
        self.original = editor.compress_container(self.raw)
        self.path.write_bytes(self.original)
        self.doc = document(self.raw, self.path)

    def test_verified_compressed_save_and_backup_use_existing_retention_api(self):
        with patch.object(editor, "backup_save", wraps=editor.backup_save) as backup:
            count, path = editor.set_human_profession(self.doc, [(WORLD, ALICE)], DOCTOR, max_backups=3)
        self.assertEqual(1, count)
        backup.assert_called_once_with(self.path, 3)
        self.assertEqual(self.original, path.read_bytes())
        raw, _ = editor.decompress_container(self.path.read_bytes())
        self.assertEqual(save([human(ALICE, (DOCTOR,))]), raw)
        self.assertEqual((DOCTOR,), editor.load_save(self.path).human_data.humans[0].professions)
        self.assertFalse(self.path.with_name(self.path.name + ".editor.tmp").exists())

    def test_no_op_does_not_write_or_back_up(self):
        with patch.object(editor, "_write_raw") as write:
            self.assertEqual((0, None), editor.set_human_profession(self.doc, [(WORLD, ALICE)], None))
        write.assert_not_called()
        self.assertEqual(self.original, self.path.read_bytes())

    def test_stale_document_is_rejected_without_backup(self):
        self.path.write_bytes(editor.compress_container(save([human(ALICE, (EXPLORER,))])))
        changed = self.path.read_bytes()
        with patch.object(editor, "_write_raw") as write:
            with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                editor.set_human_profession(self.doc, [(WORLD, ALICE)], DOCTOR)
        write.assert_not_called()
        self.assertEqual(changed, self.path.read_bytes())

    def test_backup_failure_leaves_save_untouched(self):
        with patch.object(editor, "backup_save", side_effect=OSError("Backup disk full")):
            with self.assertRaisesRegex(OSError, "Backup disk full"):
                editor.set_human_profession(self.doc, [(WORLD, ALICE)], DOCTOR)
        self.assertEqual(self.original, self.path.read_bytes())

    def test_structural_raw_write_creates_backup_and_reloads_exact_bytes(self):
        updated = save([human(ALICE, (DOCTOR,))])
        with patch.object(editor, "backup_save", wraps=editor.backup_save) as backup:
            path = editor.write_raw_changes(self.doc, updated, max_backups=3)
        backup.assert_called_once_with(self.path, 3)
        self.assertEqual(self.original, path.read_bytes())
        self.assertEqual(updated, editor.decompress_container(self.path.read_bytes())[0])
        self.assertEqual((DOCTOR,), editor.load_save(self.path).human_data.humans[0].professions)

    def test_structural_raw_write_rejects_stale_save_before_backup(self):
        changed = editor.compress_container(save([human(ALICE, (EXPLORER,))]))
        self.path.write_bytes(changed)
        with patch.object(editor, "backup_save") as backup:
            with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                editor.write_raw_changes(self.doc, save([human(ALICE, (DOCTOR,))]))
        backup.assert_not_called()
        self.assertEqual(changed, self.path.read_bytes())

    def test_structural_raw_write_leaves_save_unchanged_when_backup_fails(self):
        with patch.object(editor, "backup_save", side_effect=OSError("Backup disk full")):
            with self.assertRaisesRegex(OSError, "Backup disk full"):
                editor.write_raw_changes(self.doc, save([human(ALICE, (DOCTOR,))]))
        self.assertEqual(self.original, self.path.read_bytes())

    def test_structural_failure_does_not_call_the_writer(self):
        self.raw = save([human(ALICE, result="Success")])
        self.path.write_bytes(editor.compress_container(self.raw))
        self.doc = document(self.raw, self.path)
        with patch.object(editor, "_write_raw") as write:
            with self.assertRaises(gvas.GvasError):
                editor.set_human_profession(self.doc, [(WORLD, ALICE)], DOCTOR)
        write.assert_not_called()

    def test_human_read_failure_is_explicit_without_blocking_other_editor_tabs(self):
        with patch.object(humans, "read_humans", side_effect=gvas.GvasError("Unsupported test roster")):
            loaded = editor.load_save(self.path)
        self.assertIn("Unsupported test roster", loaded.human_data.error)
        self.assertEqual([], loaded.human_data.humans)
        self.assertEqual(self.raw, loaded.raw)

    def test_value_edits_and_several_professions_share_one_verified_write(self):
        raw = save([human(ALICE), human(BOB, (CHANCELLOR, MUTATION), result="Success")],
                   [pod(BOB, (CHANCELLOR, MUTATION))])
        self.path.write_bytes(editor.compress_container(raw))
        loaded = editor.load_save(self.path)
        entries = {entry.name: entry for entry in loaded.entries}
        edits = [(entries["Flags"], 18), (entries["GrowTimer"], 5.0)]
        changes = {(WORLD, ALICE): DOCTOR, (WORLD, BOB): EXPLORER}
        with patch.object(editor, "_write_raw", wraps=editor._write_raw) as write:
            changed, professions, backup = editor.save_changes(loaded, edits, changes, max_backups=3)
        self.assertEqual((2, 2), (changed, professions))
        write.assert_called_once()
        self.assertTrue(backup.exists())
        reloaded = editor.load_save(self.path)
        values = {entry.name: entry.value for entry in reloaded.entries}
        self.assertEqual({"Flags": 18, "GrowTimer": 5.0}, values)
        by_id = {h.social_id: h for h in reloaded.human_data.humans}
        self.assertEqual((DOCTOR,), by_id[ALICE].professions)
        self.assertEqual((EXPLORER,), by_id[BOB].professions)
        self.assertEqual((MUTATION,), by_id[BOB].other_traits)
        expected_traits = (MUTATION, EXPLORER)
        self.assertEqual(expected_traits, by_id[BOB].traits.values)
        self.assertEqual((expected_traits, expected_traits),
                         (by_id[BOB].pod.current.values, by_id[BOB].pod.simulated.values))

    def test_value_only_save_keeps_the_existing_api_and_no_backup(self):
        raw = save([human(ALICE)])
        self.path.write_bytes(editor.compress_container(raw))
        loaded = editor.load_save(self.path)
        flags = next(entry for entry in loaded.entries if entry.name == "Flags")
        with patch.object(editor, "backup_save") as backup:
            self.assertEqual((1, None), editor.apply_edits(loaded, [(flags, 19)], make_backup=False))
        backup.assert_not_called()
        self.assertEqual((), editor.load_save(self.path).human_data.humans[0].professions)

    def test_stat_scalar_and_profession_changes_share_one_verified_write_and_backup(self):
        raw = save([human(ALICE), human(BOB, (CHANCELLOR, MUTATION), result="Success")],
                   [pod(BOB, (CHANCELLOR, MUTATION))])
        self.path.write_bytes(editor.compress_container(raw))
        loaded = editor.load_save(self.path)
        flags = next(entry for entry in loaded.entries if entry.name == "Flags")
        stats = {
            ((WORLD, ALICE), "roster", ATTRIBUTE): 222.125,
            ((WORLD, BOB), "current", ATTRIBUTE): 333.25,
            ((WORLD, BOB), "simulated", ATTRIBUTE): 444.5,
        }
        with (patch.object(editor, "_write_raw", wraps=editor._write_raw) as write,
              patch.object(editor, "backup_save", wraps=editor.backup_save) as backup):
            changed, jobs, saved_backup = editor.save_changes(
                loaded, [(flags, 19)], {(WORLD, ALICE): DOCTOR, (WORLD, BOB): EXPLORER},
                human_attributes=stats, max_backups=3,
            )
        self.assertEqual((4, 2), (changed, jobs))
        write.assert_called_once()
        backup.assert_called_once_with(self.path, 3)
        self.assertEqual(raw, editor.decompress_container(saved_backup.read_bytes())[0])
        reloaded = editor.load_save(self.path)
        alice, bob = reloaded.human_data.humans
        self.assertEqual((DOCTOR,), alice.professions)
        self.assertEqual((MUTATION, EXPLORER), bob.traits.values)
        self.assertEqual(bob.traits.values, bob.pod.current.values)
        self.assertEqual(222.125, alice.attributes[ATTRIBUTE])
        self.assertEqual(123.75, bob.attributes[ATTRIBUTE])
        self.assertEqual(333.25, bob.pod.attributes_current[ATTRIBUTE])
        self.assertEqual(444.5, bob.pod.attributes_simulated[ATTRIBUTE])
        self.assertEqual(19, next(e.value for e in reloaded.entries if e.name == "Flags"))

    def test_stat_only_save_is_backed_up_and_reloaded_without_changing_professions(self):
        key = (WORLD, ALICE), "roster", ATTRIBUTE
        changed, jobs, backup = editor.save_changes(self.doc, (), human_attributes={key: 456.75})
        self.assertEqual((1, 0), (changed, jobs))
        self.assertEqual(self.original, backup.read_bytes())
        loaded = editor.load_save(self.path)
        self.assertEqual(456.75, loaded.human_data.humans[0].attributes[ATTRIBUTE])
        self.assertEqual((), loaded.human_data.humans[0].professions)

    def test_matching_stats_do_not_write_or_create_a_backup(self):
        with patch.object(editor, "_write_raw") as write:
            self.assertEqual((0, 0, None), editor.save_changes(
                self.doc, (), human_attributes={((WORLD, ALICE), "roster", ATTRIBUTE): 123.75}))
        write.assert_not_called()

    def test_invalid_stats_prevent_profession_and_scalar_writes(self):
        loaded = editor.load_save(self.path)
        flags = next(entry for entry in loaded.entries if entry.name == "Flags")
        with patch.object(editor, "_write_raw") as write, self.assertRaises(ValueError):
            editor.save_changes(
                loaded, [(flags, 19)], {(WORLD, ALICE): DOCTOR},
                human_attributes={((WORLD, ALICE), "roster", ATTRIBUTE): "nan"},
            )
        write.assert_not_called()
        self.assertEqual(self.original, self.path.read_bytes())

    def test_stat_save_refuses_stale_files_and_preserves_the_original_on_replace_failure(self):
        stats = {((WORLD, ALICE), "roster", ATTRIBUTE): 500}
        with patch.object(editor.os, "replace", side_effect=OSError("Replace failed")):
            with self.assertRaisesRegex(OSError, "Replace failed"):
                editor.save_changes(self.doc, (), human_attributes=stats)
        self.assertEqual(self.original, self.path.read_bytes())
        self.assertFalse(self.path.with_name(self.path.name + ".editor.tmp").exists())
        self.path.write_bytes(editor.compress_container(save([human(ALICE, (DOCTOR,))])))
        with patch.object(editor, "_write_raw") as write, self.assertRaisesRegex(RuntimeError, "changed on disk"):
            editor.save_changes(self.doc, (), human_attributes=stats)
        write.assert_not_called()


class HumanActionTests(unittest.TestCase):
    """Save Changes writes queued professions and value edits together."""

    @staticmethod
    def app(human_pending=None):
        doc = document(save([human(ALICE), human(BOB, (DOCTOR,))]))
        statuses, reads = [], []
        main_thread = threading.get_ident()

        def value(result):
            def get():
                reads.append(threading.get_ident())
                return result
            return SimpleNamespace(get=get)

        app = SimpleNamespace(
            document=doc, pending={},
            human_pending={(WORLD, ALICE): DOCTOR, (WORLD, BOB): None} if human_pending is None else human_pending,
            human_attribute_pending={},
            keep_var=value(7), confirm_save_var=value(False),
            _load=Mock(return_value=True), _refresh_values=Mock(), _populate_humans=Mock(),
            _populate_backups=Mock(), status_var=SimpleNamespace(set=statuses.append),
        )
        app._save_prompt = lambda *args: gui.LastCaretakerEditor._save_prompt(app, *args)
        return app, statuses, reads, main_thread

    def save(self, app, result=(0, 2, Path("Slot.sav.bak")), confirmed=True, **kwargs):
        calls = []

        def write(*args, **options):
            calls.append((threading.get_ident(), args, options))
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(gui, "_game_is_running", return_value=False):
            with patch.object(gui.messagebox, "askyesno", return_value=confirmed) as confirm:
                with patch.object(editor, "save_changes", side_effect=write):
                    saved = gui.LastCaretakerEditor._save(app, **kwargs)
        return saved, calls, confirm

    def test_every_queued_profession_and_value_edit_share_one_confirmed_worker_write(self):
        app, statuses, reads, main = self.app()
        entry = SimpleNamespace(offset=100)
        app.document.entries.append(entry)
        app.pending[100] = 5
        saved, calls, confirm = self.save(app, (1, 2, Path("Slot.sav.bak")), confirm=False)
        self.assertTrue(saved)
        self.assertEqual(1, len(calls))
        thread, args, options = calls[0]
        self.assertNotEqual(main, thread)
        self.assertEqual([(entry, 5)], args[1])
        self.assertEqual({(WORLD, ALICE): DOCTOR, (WORLD, BOB): None}, args[2])
        self.assertEqual({"human_attributes": {}, "make_backup": True, "max_backups": 7}, options)
        self.assertTrue(reads and all(ident == main for ident in reads))
        message = confirm.call_args.args[1]
        for text in ("1 value edit(s)", "2 human(s)", f"{ALICE}: No profession -> Doctor",
                     f"{BOB}: Doctor -> No profession", "REPLACES", "Mutations", "keep 7"):
            self.assertIn(text, message)
        app._load.assert_called_once_with(app.document.path, confirm_discard=False)
        self.assertEqual("Saved 1 edit(s) and professions for 2 human(s) (backup: Slot.sav.bak).", statuses[-1])

    def test_value_only_save_keeps_optional_confirmation_without_a_backup(self):
        app, statuses, _reads, _main = self.app({})
        entry = SimpleNamespace(offset=100)
        app.document.entries.append(entry)
        app.pending[100] = 5
        saved, calls, confirm = self.save(app, (1, 0, None), confirm=False)
        self.assertTrue(saved)
        confirm.assert_not_called()
        self.assertEqual(({}, {"human_attributes": {}, "make_backup": False, "max_backups": 7}),
                         (calls[0][1][2], calls[0][2]))
        self.assertEqual("Saved 1 edit(s).", statuses[-1])

    def test_cancel_keeps_the_queue_and_does_not_write(self):
        app, _statuses, _reads, _main = self.app()
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 300.0}
        saved, calls, confirm = self.save(app, confirmed=False, confirm=False)
        self.assertFalse(saved)
        confirm.assert_called_once()
        self.assertEqual([], calls)
        self.assertEqual(2, len(app.human_pending))
        self.assertEqual(1, len(app.human_attribute_pending))
        app._load.assert_not_called()

    def test_write_failure_is_reported_outside_busy_state_and_keeps_the_queue(self):
        app, statuses, _reads, _main = self.app()
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 300.0}
        busy = []
        with patch.object(gui.messagebox, "showerror", side_effect=lambda *_a: busy.append(gui._is_busy(app))) as error:
            saved, _calls, _confirm = self.save(app, OSError("disk full"))
        self.assertFalse(saved)
        self.assertEqual([False], busy)
        self.assertEqual("disk full", error.call_args.args[1])
        self.assertIn("failed", statuses[-1])
        self.assertEqual(2, len(app.human_pending))
        self.assertEqual(1, len(app.human_attribute_pending))
        app._load.assert_not_called()

    def test_reload_failure_is_not_reported_as_full_success(self):
        app, statuses, _reads, _main = self.app()
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 300.0}
        app._load.return_value = False
        saved, _calls, _confirm = self.save(app)
        self.assertFalse(saved)
        self.assertIn("but reload failed", statuses[-1])
        self.assertIn("Reload this save", app.document.human_data.error)
        self.assertEqual({}, app.human_pending)
        self.assertEqual({}, app.human_attribute_pending)
        app._populate_humans.assert_called_once()
        app._populate_backups.assert_called_once()

    def test_no_op_clears_the_queue_without_reloading(self):
        app, statuses, _reads, _main = self.app()
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 123.75}
        saved, _calls, _confirm = self.save(app, (0, 0, None))
        self.assertTrue(saved)
        self.assertEqual({}, app.human_pending)
        self.assertEqual({}, app.human_attribute_pending)
        self.assertIn("Nothing to save", statuses[-1])
        app._load.assert_not_called()

    def test_changed_identity_read_only_rows_and_invalid_retention_stop_before_confirmation(self):
        for reason in ("missing", "read_only", "data_error", "retention"):
            app, _statuses, _reads, _main = self.app()
            if reason == "missing":
                app.document = document(save())
            elif reason == "read_only":
                app.document.human_data.humans[0].edit_error = "Unsupported pod"
            elif reason == "data_error":
                app.document.human_data.error = "Unsupported roster"
            else:
                app.keep_var.get = Mock(side_effect=tk.TclError("invalid integer"))
            with self.subTest(reason=reason):
                with patch.object(gui.messagebox, "showerror") as error:
                    saved, calls, confirm = self.save(app)
                self.assertFalse(saved)
                error.assert_called_once()
                confirm.assert_not_called()
                self.assertEqual([], calls)
                self.assertEqual(2, len(app.human_pending))

    def test_nested_pump_cannot_start_another_save(self):
        app, statuses, reads, _main = self.app()
        app._pumping = True
        saved, calls, confirm = self.save(app)
        self.assertIsNone(saved)
        self.assertEqual(([], [], []), (reads, statuses, calls))
        confirm.assert_not_called()

    def test_queued_professions_block_repair_structural_edits_and_count_as_unsaved(self):
        queued = {(WORLD, ALICE): DOCTOR, (WORLD, BOB): None}
        app = SimpleNamespace(document=document(save()), pending={}, human_pending=queued,
                              _sync_document=Mock(return_value=True))
        with patch.object(gui.messagebox, "showwarning") as warning:
            gui.LastCaretakerEditor._repair_and_top_off(app)
        self.assertEqual("Unsaved human changes", warning.call_args.args[0])
        app._sync_document.assert_not_called()
        with patch.object(gui, "_game_is_running", return_value=False):
            with patch.object(gui.messagebox, "showwarning") as warning:
                self.assertFalse(gui.LastCaretakerEditor._structural_guard(app))
        warning.assert_called_once()
        self.assertEqual(2, gui._unsaved_edits(app))
        self.assertEqual(1, gui._unsaved_edits(SimpleNamespace(pending={1: 2})))
        titles = []
        app.pending = {100: 5}
        app.pending_var = SimpleNamespace(value="", set=lambda text: setattr(app.pending_var, "value", text))
        app.title = titles.append
        gui.LastCaretakerEditor._update_pending(app)
        self.assertEqual("1 pending edit(s) | 2 profession change(s)", app.pending_var.value)
        self.assertTrue(titles[-1].endswith(" *"))
        app.pending, app.human_pending = {}, {}
        gui.LastCaretakerEditor._update_pending(app)
        self.assertEqual("No pending edits", app.pending_var.value)
        self.assertFalse(titles[-1].endswith(" *"))

    def test_stat_only_save_always_confirms_and_uses_the_worker_and_backup(self):
        app, statuses, reads, main = self.app({})
        stats = {((WORLD, ALICE), "roster", ATTRIBUTE): 1234.0}
        app.human_attribute_pending = stats
        saved, calls, confirm = self.save(app, (1, 0, Path("Slot.sav.bak")), confirm=False)
        self.assertTrue(saved)
        self.assertEqual(1, len(calls))
        self.assertNotEqual(main, calls[0][0])
        self.assertEqual({"human_attributes": stats, "make_backup": True, "max_backups": 7}, calls[0][2])
        self.assertIsNot(stats, calls[0][2]["human_attributes"])
        self.assertTrue(all(ident == main for ident in reads))
        message = confirm.call_args.args[1]
        for text in ("1 raw stat field(s)", ALICE, "Height", "Roster (raw)", "123.75 -> 1234.0",
                     "independently", "not converted", "keep 7"):
            self.assertIn(text, message)
        self.assertNotIn("REPLACES", message)
        self.assertNotIn("Attributes and growth stats are not changed", message)
        app._load.assert_called_once()
        self.assertIn("Saved 1 edit(s)", statuses[-1])

    def test_stat_changes_block_maintenance_and_count_as_unsaved_edits(self):
        app, _statuses, _reads, _main = self.app({})
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 1234.0}
        app._sync_document = Mock(return_value=True)
        with patch.object(gui.messagebox, "showwarning") as warning:
            gui.LastCaretakerEditor._repair_and_top_off(app)
        warning.assert_called_once()
        app._sync_document.assert_not_called()
        with (patch.object(gui, "_game_is_running", return_value=False),
              patch.object(gui.messagebox, "showwarning") as warning):
            self.assertFalse(gui.LastCaretakerEditor._structural_guard(app))
        warning.assert_called_once()
        self.assertEqual(1, gui._unsaved_edits(app))
        app.pending_var = Mock()
        app.title = Mock()
        gui.LastCaretakerEditor._update_pending(app)
        app.pending_var.set.assert_called_once_with("1 human stat field(s)")
        self.assertTrue(app.title.call_args.args[0].endswith(" *"))

    def test_stat_only_save_cannot_bypass_the_game_running_check(self):
        app, _statuses, _reads, _main = self.app({})
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 1234.0}
        with (patch.object(gui, "_game_is_running", return_value=True),
              patch.object(gui.messagebox, "showerror") as error,
              patch.object(editor, "save_changes") as write):
            self.assertFalse(gui.LastCaretakerEditor._save(app))
        self.assertEqual("Game is running", error.call_args.args[0])
        write.assert_not_called()
        self.assertEqual(1, len(app.human_attribute_pending))


class HumanGuiTests(unittest.TestCase):
    def setUp(self):
        mock = patch.object(gui, "_load_settings", return_value={})
        mock.start()
        self.addCleanup(mock.stop)
        for name in ("_load_startup_save", "_refresh_saves", "_open_full_screen", "_write_settings"):
            mock = patch.object(gui.LastCaretakerEditor, name, return_value=None)
            mock.start()
            self.addCleanup(mock.stop)
        self.app = gui.LastCaretakerEditor()
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.app.document = document(save(
            [human(ALICE, (EXPLORER,)), human(BOB, (CHANCELLOR, MUTATION), result="Success")],
            [pod(BOB, (CHANCELLOR, MUTATION), position=(100.0, 200.0, 300.0))],
            player_position=(100.0, 200.0, 0.0),
        ))
        self.app._populate_humans()
        self.app.update_idletasks()

    def select(self, social_id):
        iid = next(iid for iid, human in self.app.human_rows.items() if human.social_id == social_id)
        self.app.human_tree.selection_set(iid)
        self.app._show_human_selection()
        return iid

    def test_roster_filters_by_job_id_location_and_exclusions(self):
        app = self.app
        for query, expected in (("frontier", 1), (BOB, 1), ("-frontier", 1), ("not-present", 0), ("", 2)):
            app.human_filter.set(query)
            app._apply_human_filter()
            self.assertEqual(expected, len(app.human_tree.get_children()), query)
        app.human_location_filter.set("In pod")
        app._populate_humans()
        self.assertEqual([BOB], [h.social_id for h in app.human_rows.values()])
        app.human_location_filter.set("All")
        app._populate_humans()
        self.assertEqual(2, len(app.human_rows))

    def test_selection_shows_distinct_roster_and_pod_values_with_an_edit_action(self):
        self.select(BOB)
        app = self.app
        self.assertEqual("Existential Chancellor", app.human_profession_var.get())
        self.assertEqual("readonly", str(app.human_profession_combo["state"]))
        self.assertEqual("disabled", str(app.human_save_button["state"]))
        values = app.human_attribute_tree.item(ATTRIBUTE, "values")
        self.assertEqual(("123.75", "901.25", "1000.5"), values[1:])
        human_iid = app.human_tree.selection()[0]
        self.assertEqual("1.0, 2.0, 3.0", app.human_tree.set(human_iid, "pod_coordinates"))
        self.assertEqual("3", app.human_tree.set(human_iid, "pod_distance"))
        self.assertIn(BOB, app.human_details_var.get())
        self.assertIn("BP_Module_Human_Pod", app.human_details_var.get())
        self.assertTrue(app.human_attribute_tree.bind("<Double-1>"))
        self.assertTrue(app.human_attribute_tree.bind("<Return>"))
        self.assertEqual("disabled", str(app.human_stat_button["state"]))
        app.human_attribute_tree.selection_set(ATTRIBUTE)
        app._update_human_action()
        self.assertEqual("normal", str(app.human_stat_button["state"]))
        self.assertEqual({}, app.pending)

    def test_mixed_selection_queues_an_explicit_profession_for_every_selected_human(self):
        app = self.app
        app.human_tree.selection_set(app.human_tree.get_children())
        app._show_human_selection()
        self.assertEqual("", app.human_profession_var.get())
        self.assertEqual("disabled", str(app.human_save_button["state"]))
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        self.assertEqual({(WORLD, ALICE): DOCTOR, (WORLD, BOB): DOCTOR}, app.human_pending)
        self.assertEqual("normal", str(app.human_save_button["state"]))
        self.assertEqual("normal", str(app.human_discard_button["state"]))
        self.assertEqual("Doctor", app.human_profession_var.get())
        self.assertIn("2 humans selected", app.human_details_var.get())
        self.assertEqual({}, app.pending)

    def test_queued_professions_survive_selection_and_filter_changes_until_one_save(self):
        app = self.app
        alice = self.select(ALICE)
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        self.select(BOB)
        self.assertEqual("Existential Chancellor", app.human_profession_var.get())
        app.human_profession_var.set("Frontier Explorer")
        app._queue_human_profession()
        self.select(ALICE)
        self.assertEqual("Doctor", app.human_profession_var.get())
        self.assertIn("Unsaved profession", app.human_details_var.get())
        app.human_filter.set(BOB)
        app._apply_human_filter()
        app._clear_filters("human", app._apply_human_filter)
        expected = {(WORLD, ALICE): DOCTOR, (WORLD, BOB): EXPLORER}
        self.assertEqual(expected, app.human_pending)
        self.assertEqual("Doctor (unsaved; was Frontier Explorer)", app.human_tree.set(alice, "professions"))
        self.assertIn("2 profession change(s)", app.pending_var.get())
        self.assertIn("Queued: 2 profession(s), 0 stat(s)", app.human_summary_var.get())
        covered = [app.committee_tree.item(iid, "values") for iid in app.committee_tree.get_children()
                   if app.committee_tree.set(iid, "status") == "Present"]
        self.assertEqual([("Doctor", ALICE)], [(row[2], row[8]) for row in covered])
        with patch.object(gui, "_game_is_running", return_value=False):
            with patch.object(gui.messagebox, "askyesno", return_value=True) as confirm:
                with patch.object(editor, "save_changes", return_value=(0, 2, None)) as write:
                    with patch.object(app, "_load", return_value=True):
                        self.assertTrue(app._save())
        write.assert_called_once()
        self.assertEqual(expected, write.call_args.args[2])
        confirm.assert_called_once()

    def test_choosing_the_saved_profession_unqueues_and_discard_restores_the_roster(self):
        app = self.app
        alice = self.select(ALICE)
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        app.human_profession_var.set("Frontier Explorer")
        app._queue_human_profession()
        self.assertEqual({}, app.human_pending)
        self.assertEqual("disabled", str(app.human_save_button["state"]))
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            app._discard_human_changes()
        self.assertEqual(1, len(app.human_pending))
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            app._discard_human_changes()
        self.assertEqual({}, app.human_pending)
        self.assertEqual("Frontier Explorer", app.human_tree.set(alice, "professions"))
        self.assertEqual("Frontier Explorer", app.human_profession_var.get())
        self.assertEqual("No pending edits", app.pending_var.get())
        self.assertEqual("disabled", str(app.human_discard_button["state"]))

    def test_loading_a_save_clears_queued_professions_after_discard_confirmation(self):
        app = self.app
        self.select(ALICE)
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        app.human_attribute_pending = {((WORLD, ALICE), "roster", ATTRIBUTE): 42.0}
        with patch.object(gui.messagebox, "askyesno", return_value=False) as confirm:
            with patch.object(editor, "load_save") as load:
                self.assertFalse(app._load(app.document.path))
        confirm.assert_called_once()
        load.assert_not_called()
        with patch.object(editor, "load_save", return_value=document(save([human(ALICE)]))):
            self.assertTrue(app._load(app.document.path, confirm_discard=False))
        self.assertEqual({}, app.human_pending)
        self.assertEqual({}, app.human_attribute_pending)
        self.assertEqual("disabled", str(app.human_save_button["state"]))

    def test_error_empty_and_read_only_states_disable_mutation_controls(self):
        app = self.app
        self.select(ALICE)
        app.document.human_data.humans[0].edit_error = "Unknown pod layout"
        app._populate_humans()
        self.assertEqual("disabled", str(app.human_save_button["state"]))
        self.assertEqual("disabled", str(app.human_profession_combo["state"]))
        self.assertEqual("disabled", str(app.human_stat_button["state"]))
        self.assertIn("Unknown pod layout", app.human_details_var.get())
        app.human_profession_var.set("Doctor")
        with patch.object(gui.messagebox, "showerror") as error:
            app._queue_human_profession()
        error.assert_called_once()
        self.assertEqual({}, app.human_pending)
        app.document.human_data.error = "Unsupported human layout"
        app._populate_humans()
        self.assertEqual((), app.human_tree.get_children())
        self.assertEqual((), app.committee_tree.get_children())
        self.assertEqual("disabled", str(app.human_save_button["state"]))
        self.assertIn("Unsupported human layout", app.human_summary_var.get())
        app.document = document(save())
        app._populate_humans()
        self.assertIn("No grown humans", app.human_summary_var.get())
        self.assertEqual("disabled", str(app.human_save_button["state"]))

    def edit_stat(self, social_id, values):
        self.select(social_id)
        self.app.human_attribute_tree.selection_set(ATTRIBUTE)
        with patch.object(gui, "_HumanStatDialog", return_value=SimpleNamespace(result=values)) as dialog:
            self.app._edit_human_attribute()
        return dialog

    def test_stat_changes_for_several_humans_survive_selection_filters_and_sorting(self):
        app = self.app
        self.edit_stat(ALICE, {"roster": 500.25})
        self.edit_stat(BOB, {"roster": 123.75, "current": 800.5, "simulated": 900.75})
        expected = {
            ((WORLD, ALICE), "roster", ATTRIBUTE): 500.25,
            ((WORLD, BOB), "current", ATTRIBUTE): 800.5,
            ((WORLD, BOB), "simulated", ATTRIBUTE): 900.75,
        }
        app.human_filter.set(ALICE)
        app._apply_human_filter()
        app._clear_filters("human", app._apply_human_filter)
        app._sort_tree(app.human_tree, "location")
        bob = self.select(BOB)
        self.assertEqual(expected, app.human_attribute_pending)
        self.assertEqual(("123.75", "800.5 *", "900.75 *"), app.human_attribute_tree.item(ATTRIBUTE, "values")[1:])
        self.assertIn("(unsaved stats)", app.human_tree.set(bob, "location"))
        self.assertEqual("3 human stat field(s)", app.pending_var.get())
        self.assertIn("0 profession(s), 3 stat(s)", app.human_summary_var.get())
        self.assertEqual("normal", str(app.human_save_button["state"]))
        self.assertEqual("normal", str(app.human_discard_button["state"]))
        self.assertEqual(901.25, app.human_rows[bob].pod.attributes_current[ATTRIBUTE])
        dialog = self.edit_stat(BOB, None)
        self.assertEqual({"roster": 123.75, "current": 800.5, "simulated": 900.75}, dialog.call_args.args[3])
        self.assertEqual(expected, app.human_attribute_pending)

    def test_saved_values_unqueue_stats_and_discard_clears_both_human_queues_only(self):
        app = self.app
        self.edit_stat(ALICE, {"roster": 500.25})
        self.edit_stat(ALICE, {"roster": 123.75})
        self.assertEqual({}, app.human_attribute_pending)
        self.assertEqual("disabled", str(app.human_save_button["state"]))
        self.edit_stat(ALICE, {"roster": 500.25})
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        app.pending[123] = 5
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            app._discard_human_changes()
        self.assertEqual(1, len(app.human_attribute_pending))
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            app._discard_human_changes()
        self.assertEqual({}, app.human_pending)
        self.assertEqual({}, app.human_attribute_pending)
        self.assertEqual({123: 5}, app.pending)
        self.assertEqual("123.75", app.human_attribute_tree.set(ATTRIBUTE, "roster"))
        self.assertEqual("disabled", str(app.human_discard_button["state"]))

    def test_stat_dialog_keeps_double_precision_and_validates_every_field_before_queuing(self):
        self.select(BOB)
        entry = self.app.human_rows[self.app.human_tree.selection()[0]]
        exact = 1.2345678901234567
        dialog = SimpleNamespace(
            human=entry, asset=ATTRIBUTE, initial={"roster": exact, "current": 901.25, "simulated": 1000.5},
            focus_source="current", fields={}, parsed={}, result=None,
        )
        frame = gui.ttk.Frame(self.app)
        self.addCleanup(frame.destroy)
        focus = gui._HumanStatDialog.body(dialog, frame)
        self.assertIs(dialog.fields["current"], focus)
        self.assertEqual(str(exact), dialog.fields["roster"].get())
        dialog.fields["simulated"].delete(0, tk.END)
        dialog.fields["simulated"].insert(0, "nan")
        with patch.object(gui.messagebox, "showerror") as error:
            self.assertFalse(gui._HumanStatDialog.validate(dialog))
        self.assertIn("Pod simulated (raw)", error.call_args.args[1])
        self.assertEqual({}, dialog.parsed)
        self.assertIsNone(dialog.result)
        dialog.fields["simulated"].delete(0, tk.END)
        dialog.fields["simulated"].insert(0, "-42.5")
        self.assertTrue(gui._HumanStatDialog.validate(dialog))
        gui._HumanStatDialog.apply(dialog)
        self.assertEqual({"roster": exact, "current": 901.25, "simulated": -42.5}, dialog.result)
        self.assertEqual({}, self.app.human_attribute_pending)

    def test_double_click_targets_the_clicked_stat_source_and_missing_sources_are_not_editable(self):
        app = self.app
        event = SimpleNamespace(type=tk.EventType.ButtonPress, x=10, y=10)
        for social_id, column, expected_source in ((BOB, "#3", "current"), (ALICE, "#2", "roster")):
            self.select(social_id)
            with (patch.object(app.human_attribute_tree, "identify_region", return_value="cell"),
                  patch.object(app.human_attribute_tree, "identify_row", return_value=ATTRIBUTE),
                  patch.object(app.human_attribute_tree, "identify_column", return_value=column),
                  patch.object(gui, "_HumanStatDialog", return_value=SimpleNamespace(result=None)) as dialog):
                app._edit_human_attribute(event)
            self.assertEqual(expected_source, dialog.call_args.args[4])
        with (patch.object(app.human_attribute_tree, "identify_region", return_value="cell"),
              patch.object(app.human_attribute_tree, "identify_row", return_value=ATTRIBUTE),
              patch.object(app.human_attribute_tree, "identify_column", return_value="#3"),
              patch.object(gui.messagebox, "showinfo") as info,
              patch.object(gui, "_HumanStatDialog") as dialog):
            app._edit_human_attribute(event)
        dialog.assert_not_called()
        self.assertEqual("Stat unavailable", info.call_args.args[0])

    def test_read_only_multi_selection_and_worker_pump_cannot_start_a_stat_edit(self):
        app = self.app
        self.select(ALICE)
        app.human_attribute_tree.selection_set(ATTRIBUTE)
        app._pumping = True
        with patch.object(gui, "_HumanStatDialog") as dialog:
            app._edit_human_attribute()
        dialog.assert_not_called()
        app._pumping = False
        app.document.human_data.humans[0].edit_error = "Unknown pod"
        with (patch.object(gui.messagebox, "showerror") as error,
              patch.object(gui, "_HumanStatDialog") as dialog):
            app._edit_human_attribute()
        dialog.assert_not_called()
        self.assertEqual("Unknown pod", error.call_args.args[1])
        app.human_tree.selection_set(app.human_tree.get_children())
        app._show_human_selection()
        self.assertEqual("disabled", str(app.human_stat_button["state"]))
        with patch.object(gui, "_HumanStatDialog") as dialog:
            app._edit_human_attribute()
        dialog.assert_not_called()

    def test_failed_load_and_cancelled_close_keep_unsaved_stats(self):
        app = self.app
        self.edit_stat(ALICE, {"roster": 500.25})
        queued = dict(app.human_attribute_pending)
        with (patch.object(editor, "load_save", side_effect=OSError("Read failed")),
              patch.object(gui.messagebox, "showerror") as error):
            self.assertFalse(app._load(app.document.path, confirm_discard=False))
        self.assertEqual(queued, app.human_attribute_pending)
        error.assert_called_once()
        with (patch.object(gui.messagebox, "askyesno", return_value=False) as confirm,
              patch.object(app, "destroy") as destroy):
            app._on_close()
        confirm.assert_called_once()
        destroy.assert_not_called()
        self.assertEqual(queued, app.human_attribute_pending)

    def test_save_writes_all_queued_stats_and_professions_then_reloads_and_clears_them(self):
        app = self.app
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "Slot.sav"
        path.write_bytes(editor.compress_container(app.document.raw))
        self.assertTrue(app._load(path, confirm_discard=False))
        self.edit_stat(ALICE, {"roster": 500.25})
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        self.edit_stat(BOB, {"roster": 123.75, "current": 800.5, "simulated": 900.75})
        app.human_profession_var.set("Frontier Explorer")
        app._queue_human_profession()
        with (patch.object(gui, "_game_is_running", return_value=False),
              patch.object(gui.messagebox, "askyesno", return_value=True),
              patch.object(editor, "_write_raw", wraps=editor._write_raw) as write):
            self.assertTrue(app._save())
        write.assert_called_once()
        self.assertEqual(1, len(editor.list_backups(path)))
        self.assertEqual({}, app.human_pending)
        self.assertEqual({}, app.human_attribute_pending)
        self.assertEqual("No pending edits", app.pending_var.get())
        alice, bob = app.document.human_data.humans
        self.assertEqual((500.25, (DOCTOR,)), (alice.attributes[ATTRIBUTE], alice.professions))
        self.assertEqual((800.5, 900.75, (EXPLORER,)), (
            bob.pod.attributes_current[ATTRIBUTE], bob.pod.attributes_simulated[ATTRIBUTE], bob.professions))
        self.assertEqual(("123.75", "800.5", "900.75"), app.human_attribute_tree.item(ATTRIBUTE, "values")[1:])
        self.assertEqual("disabled", str(app.human_save_button["state"]))

    def test_committee_roles_are_filterable_and_count_jobs_not_pod_humans(self):
        app = self.app
        self.assertEqual(40, len(app.committee_tree.get_children()))
        app.committee_filter.set("frontier")
        app._apply_committee_filter()
        values = [app.committee_tree.item(iid, "values") for iid in app.committee_tree.get_children()]
        self.assertEqual(4, len(values))
        self.assertTrue(all(row[1] == "1/4" for row in values))
        self.assertEqual(1, sum(row[4] == "Present" for row in values))
        app._clear_filters("committee", app._apply_committee_filter)
        self.assertEqual(40, len(app.committee_tree.get_children()))

    def test_unknown_saved_committees_are_visible(self):
        self.app.document.human_data.committee_states[WORLD, "FUTURE"] = "EVoyageCommitteeState::Waiting"
        self.app._populate_committees()
        row = next(self.app.committee_tree.item(iid, "values") for iid in self.app.committee_tree.get_children()
                   if self.app.committee_tree.set(iid, "committee") == "FUTURE")
        self.assertEqual("Unknown", row[1])
        self.assertEqual("Waiting", row[6])

    def test_reload_keeps_social_id_selection_across_structural_offset_changes(self):
        app = self.app
        iid = self.select(BOB)
        raw, _ = humans.replace_professions(app.document.raw, [(WORLD, ALICE)], CHANCELLOR)
        updated = document(raw, app.document.path)
        with patch.object(editor, "load_save", return_value=updated):
            self.assertTrue(app._load(app.document.path, confirm_discard=False))
        self.assertEqual((iid,), app.human_tree.selection())
        self.assertIn(BOB, app.human_details_var.get())
        self.assertIn(app.human_tree, app._trees())
        self.assertIn(app.committee_tree, app._trees())

    def test_human_load_warning_is_separate_from_inventory_warnings(self):
        app = self.app
        doc = document(save())
        doc.human_data.error = "Unsupported test roster"
        busy = []
        with patch.object(editor, "load_save", return_value=doc):
            with patch.object(gui.messagebox, "showwarning", side_effect=lambda *_a: busy.append(gui._is_busy(app))) as warning:
                self.assertTrue(app._load(doc.path, confirm_discard=False))
        self.assertEqual([False], busy)
        self.assertEqual("Human data", warning.call_args.args[0])
        self.assertIn("Unsupported test roster", app.status_var.get())

    def test_human_save_services_events_without_allowing_reentrant_edits(self):
        app = self.app
        self.select(ALICE)
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        ticks = []
        timer = None

        def heartbeat():
            nonlocal timer
            ticks.append(time.monotonic())
            app.human_profession_var.set("Existential Chancellor")
            app._queue_human_profession()
            app._save()
            timer = app.after(10, heartbeat)

        def write(*_args, **_kwargs):
            time.sleep(0.18)
            return 0, 1, Path("Slot.sav.bak")

        timer = app.after(10, heartbeat)
        self.addCleanup(lambda: app.after_cancel(timer))
        with patch.object(gui, "_game_is_running", return_value=False):
            with patch.object(app, "_load", return_value=True):
                with patch.object(gui.messagebox, "askyesno", return_value=True):
                    with patch.object(editor, "save_changes", side_effect=write) as worker:
                        app._save()
        worker.assert_called_once()
        self.assertEqual({(WORLD, ALICE): DOCTOR}, worker.call_args.args[2])
        self.assertGreaterEqual(len(ticks), 2)

    def test_controls_fit_default_and_minimum_window_sizes(self):
        app = self.app
        app.attributes("-alpha", 0.0)
        app.deiconify()
        app.notebook.select(app.humans_tab)
        self.select(BOB)
        app.human_attribute_tree.selection_set(ATTRIBUTE)
        app.human_profession_var.set("Doctor")
        app._queue_human_profession()
        app.human_attribute_pending = {((WORLD, BOB), "current", ATTRIBUTE): 1234.0}
        app._refresh_human_rows()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        for geometry in ("1360x820", "1040x680"):
            for tab in (0, 1):
                with self.subTest(geometry=geometry, tab=tab):
                    app.geometry(geometry)
                    app.human_notebook.select(tab)
                    app.update()
                    left, top = app.humans_tab.winfo_rootx(), app.humans_tab.winfo_rooty()
                    right = left + app.humans_tab.winfo_width()
                    bottom = top + app.humans_tab.winfo_height()
                    for widget in descendants(app.humans_tab):
                        if not widget.winfo_ismapped():
                            continue
                        self.assertGreaterEqual(widget.winfo_rootx(), left, str(widget))
                        self.assertGreaterEqual(widget.winfo_rooty(), top, str(widget))
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, str(widget))
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom, str(widget))
                    if tab == 0:
                        self.assertTrue(app.human_save_button.winfo_ismapped())
                        self.assertTrue(app.human_discard_button.winfo_ismapped())
                        self.assertTrue(app.human_stat_button.winfo_ismapped())
                        self.assertGreaterEqual(app.human_attribute_tree.winfo_height(), 60)

    def test_status_bar_stays_visible_at_default_and_minimum_window_sizes(self):
        app = self.app
        app.attributes("-alpha", 0.0)
        app.deiconify()
        for geometry in ("1360x820", "1040x680"):
            for tab in (app.inventory_tab, app.humans_tab, app.scalars_tab):
                with self.subTest(geometry=geometry, tab=str(tab)):
                    app.geometry(geometry)
                    app.notebook.select(tab)
                    app.update()
                    bar = app.status_bar
                    self.assertTrue(bar.winfo_ismapped())
                    self.assertGreater(bar.winfo_height(), 0)
                    self.assertLessEqual(bar.winfo_y() + bar.winfo_height(), app.winfo_height())
                    self.assertLessEqual(
                        app.notebook.winfo_y() + app.notebook.winfo_height(), bar.winfo_y())


if __name__ == "__main__":
    unittest.main()
