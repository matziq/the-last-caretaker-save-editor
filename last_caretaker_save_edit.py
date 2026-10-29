"""The Last Caretaker save parser, editor library, and CLI."""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:
    import voyage_gvas
except ImportError:  # pragma: no cover - direct execution from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import voyage_gvas

import voyage_humans


DEFAULT_SAVE_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    / "Voyage" / "Saved" / "SaveGames"
)
CONTAINER_MAGIC = struct.pack("<II", 0x9E2A83C1, 0x22222222)
DEFAULT_CHUNK_SIZE = 128 * 1024
DEFAULT_COMPRESSION_LEVEL = 1
HEADER_SIZE = 49
EDITABLE_TYPES = ("IntProperty", "FloatProperty", "DoubleProperty", "BoolProperty")
_TYPE_WIDTHS = {
    "IntProperty": 4,
    "FloatProperty": 4,
    "DoubleProperty": 8,
    "BoolProperty": 0,
}
_MAX_NAME_LEN = 160


class SaveFormatError(ValueError):
    pass


@dataclass
class Entry:
    index: int
    offset: int
    name: str
    type: str
    value: int | float | bool
    context: str = ""


@dataclass
class TextAnchor:
    offset: int
    field: str
    value: str


@dataclass
class InventoryItem:
    index: int
    asset: str
    category: str
    actor: str
    location: str
    anchor_offset: int
    entries: dict[str, Entry]

    @property
    def count(self) -> int:
        entry = self.entries.get("ItemCount")
        return int(entry.value) if entry else 0


@dataclass
class ResourceAmount:
    resource: str
    actor: str
    asset: str
    location: str
    map_offset: int
    entry: Entry
    health: Entry | None = None
    extra_damage: Entry | None = None


@dataclass
class SaveDocument:
    path: Path
    raw: bytes
    entries: list[Entry]
    anchors: list[TextAnchor]
    items: list[InventoryItem]
    chunk_size: int
    compressed_size: int
    mirrors: list[tuple[int, int, int]] = field(default_factory=list)
    resources: list[ResourceAmount] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    human_data: voyage_humans.HumanData = field(default_factory=voyage_humans.HumanData)


def _fstring_tag(name: str) -> bytes:
    return struct.pack("<i", len(name) + 1) + name.encode("ascii") + b"\0"


def _strip_blueprint_suffix(name: str) -> str:
    parts = name.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit() and len(parts[2]) >= 8:
        if all(c in "0123456789abcdefABCDEF" for c in parts[2]):
            return parts[0]
    return name


def _read_fstring(data: bytes, offset: int) -> str | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    length = struct.unpack_from("<i", data, offset)[0]
    if length == 0:
        return ""
    if length < 0:
        chars = -length
        end = offset + 4 + chars * 2
        if chars > 4096 or end > len(data):
            return None
        payload = data[offset + 4:end]
        if payload[-2:] != b"\0\0":
            return None
        try:
            return payload[:-2].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    end = offset + 4 + length
    if length > 4096 or end > len(data):
        return None
    payload = data[offset + 4:end]
    if not payload or payload[-1] != 0:
        return None
    try:
        text = payload[:-1].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 0x20 and c not in "\t\r\n" for c in text):
        return None
    return text


def _recover_name_before(data: bytes, type_offset: int) -> str | None:
    if type_offset < 6 or data[type_offset - 1] != 0:
        return None
    for length in range(1, _MAX_NAME_LEN + 1):
        body_start = type_offset - 1 - length
        length_offset = body_start - 4
        if length_offset < 0:
            break
        if struct.unpack_from("<i", data, length_offset)[0] != length + 1:
            continue
        body = data[body_start:type_offset - 1]
        if all(0x20 <= byte < 0x7F for byte in body):
            return _strip_blueprint_suffix(body.decode("ascii"))
    return None


def decompress_container(data: bytes) -> tuple[bytes, int]:
    """Decode Voyage's concatenated Unreal Oodle or zlib chunks."""
    offset = 0
    output = bytearray()
    chunk_size: int | None = None
    block = 0
    while offset < len(data):
        if offset + HEADER_SIZE > len(data):
            raise SaveFormatError(f"Truncated compressed block header at 0x{offset:X}")
        if data[offset:offset + 8] != CONTAINER_MAGIC:
            raise SaveFormatError(f"Invalid compressed block magic at 0x{offset:X}")
        this_chunk_size = struct.unpack_from("<Q", data, offset + 8)[0]
        method = data[offset + 16]
        total_compressed = struct.unpack_from("<Q", data, offset + 17)[0]
        total_raw = struct.unpack_from("<Q", data, offset + 25)[0]
        part_compressed = struct.unpack_from("<Q", data, offset + 33)[0]
        part_raw = struct.unpack_from("<Q", data, offset + 41)[0]
        if method not in (2, 3):
            raise SaveFormatError(f"Unsupported compression method {method} in block {block}")
        if total_compressed != part_compressed or total_raw != part_raw:
            raise SaveFormatError(f"Unsupported multi-part compressed block {block}")
        if this_chunk_size < 1 or not 0 < part_raw <= this_chunk_size:
            raise SaveFormatError(f"Invalid uncompressed size in block {block}")
        end = offset + HEADER_SIZE + part_compressed
        if end > len(data):
            raise SaveFormatError(f"Truncated compressed payload in block {block}")
        payload = data[offset + HEADER_SIZE:end]
        if method == 2:
            try:
                from ooz import decompress as oodle_decompress
            except ImportError as exc:
                raise SaveFormatError(
                    "This save uses Oodle compression, which requires pyooz. "
                    "Close the editor and install requirements.txt using the same "
                    "Python interpreter as run.bat: "
                    "python -m pip install -r requirements.txt"
                ) from exc
            try:
                decoded = oodle_decompress(payload, part_raw)
            except RuntimeError as exc:
                raise SaveFormatError(f"Invalid Oodle payload in block {block}: {exc}") from exc
        else:
            try:
                decoded = zlib.decompress(payload)
            except zlib.error as exc:
                raise SaveFormatError(f"Invalid zlib payload in block {block}: {exc}") from exc
        if len(decoded) != part_raw:
            raise SaveFormatError(
                f"Block {block} expands to {len(decoded)} bytes, expected {part_raw}"
            )
        if chunk_size is None:
            chunk_size = this_chunk_size
        elif this_chunk_size != chunk_size:
            raise SaveFormatError("Compressed blocks use inconsistent chunk sizes")
        output.extend(decoded)
        offset = end
        block += 1
    if not output:
        raise SaveFormatError("Save contains no compressed blocks")
    return bytes(output), chunk_size or DEFAULT_CHUNK_SIZE


def compress_container(raw: bytes, chunk_size: int = DEFAULT_CHUNK_SIZE,
                     compression_level: int = DEFAULT_COMPRESSION_LEVEL) -> bytes:
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    if not 0 <= compression_level <= 9:
        raise ValueError("compression_level must be between 0 and 9")
    output = bytearray()
    for start in range(0, len(raw), chunk_size):
        chunk = raw[start:start + chunk_size]
        # The save is rewritten in full on every write, so prefer a faster
        # compression level here to avoid a disproportionate slowdown as the save
        # grows with more inventory edits. This keeps the file format identical
        # while making repeated save operations noticeably snappier.
        compressed = zlib.compress(chunk, level=compression_level)
        output.extend(CONTAINER_MAGIC)
        output.extend(struct.pack("<Q", chunk_size))
        output.append(3)
        output.extend(struct.pack("<Q", len(compressed)))
        output.extend(struct.pack("<Q", len(chunk)))
        output.extend(struct.pack("<Q", len(compressed)))
        output.extend(struct.pack("<Q", len(chunk)))
        output.extend(compressed)
    return bytes(output)


def validate_raw_save(raw: bytes) -> None:
    if len(raw) < 8:
        raise SaveFormatError("Decompressed save is too short")
    declared = struct.unpack_from("<I", raw, 0)[0]
    if declared != len(raw) - 4:
        raise SaveFormatError(
            f"GVAS envelope declares {declared} bytes, found {len(raw) - 4}"
        )
    if raw[4:8] != b"GVAS":
        raise SaveFormatError("Decompressed payload does not contain GVAS")


def scan_scalars(data: bytes) -> list[Entry]:
    entries: list[Entry] = []
    seen: set[int] = set()
    for property_type in EDITABLE_TYPES:
        tag = _fstring_tag(property_type)
        expected_size = _TYPE_WIDTHS[property_type]
        search_from = 0
        while True:
            type_offset = data.find(tag, search_from)
            if type_offset < 0:
                break
            search_from = type_offset + len(tag)
            name = _recover_name_before(data, type_offset)
            if not name:
                continue
            after_type = type_offset + len(tag)
            if after_type + 10 > len(data):
                continue
            array_index, size = struct.unpack_from("<ii", data, after_type)
            if array_index != 0 or size != expected_size:
                continue
            if property_type == "BoolProperty":
                value_offset = after_type + 8
                if data[value_offset] not in (0, 1) or data[after_type + 9] != 0:
                    continue
                value: int | float | bool = bool(data[value_offset])
            else:
                if data[after_type + 8] != 0:
                    continue
                value_offset = after_type + 9
                if value_offset + expected_size > len(data):
                    continue
                if property_type == "IntProperty":
                    value = struct.unpack_from("<i", data, value_offset)[0]
                elif property_type == "FloatProperty":
                    value = struct.unpack_from("<f", data, value_offset)[0]
                else:
                    value = struct.unpack_from("<d", data, value_offset)[0]
            if value_offset in seen:
                continue
            seen.add(value_offset)
            entries.append(Entry(0, value_offset, name, property_type, value))
    entries.sort(key=lambda entry: entry.offset)
    for index, entry in enumerate(entries):
        entry.index = index
    return entries


def scan_text_anchors(data: bytes) -> list[TextAnchor]:
    anchors: list[TextAnchor] = []
    for property_type in (
        "StrProperty", "NameProperty", "ObjectProperty", "SoftObjectProperty"
    ):
        tag = _fstring_tag(property_type)
        search_from = 0
        while True:
            type_offset = data.find(tag, search_from)
            if type_offset < 0:
                break
            search_from = type_offset + len(tag)
            field = _recover_name_before(data, type_offset)
            if not field:
                continue
            after_type = type_offset + len(tag)
            if after_type + 9 > len(data):
                continue
            array_index, size = struct.unpack_from("<ii", data, after_type)
            if array_index != 0 or size < 4 or data[after_type + 8] != 0:
                continue
            value_offset = after_type + 9
            value = _read_fstring(data, value_offset)
            if value:
                anchors.append(TextAnchor(value_offset, field, value))
    anchors.sort(key=lambda anchor: anchor.offset)
    return anchors


def _short_asset(value: str) -> str:
    leaf = value.rsplit("/", 1)[-1].split(".", 1)[0]
    for prefix in ("DA_Item_", "DA_Material_", "DA_Equipment_", "DA_", "BP_"):
        if leaf.startswith(prefix):
            leaf = leaf[len(prefix):]
            break
    return leaf.replace("_", " ")


PLAYER_OWNER = "pawns"

LOC_BACKPACK = "Player Backpack"
LOC_HOLSTER = "Player Holster"
LOC_EQUIPPED = "Player Equipped"
LOC_CATALOG = "Player Catalog"
LOC_MODULE = "Player Module"
LOC_PLAYER_CRATE = "Player Crate"
LOC_CONTAINER = "Container/Crate"
LOC_ENVIRONMENT = "Environment"
LOC_OTHER = "Other"

#: Components the player actually carries.  Bulk actions stay inside this set so
#: recipe catalog and the module mirror are never rewritten. Player-created
#: PlayerBox storage is also owned by the player and should be included.
CARRIED_LOCATIONS = frozenset({
    LOC_BACKPACK, LOC_HOLSTER, LOC_EQUIPPED, LOC_PLAYER_CRATE,
})

#: Every location the inventory filter can offer, in display order.
ALL_LOCATIONS = (
    LOC_BACKPACK, LOC_HOLSTER, LOC_EQUIPPED, LOC_CATALOG, LOC_MODULE,
    LOC_PLAYER_CRATE, LOC_CONTAINER, LOC_ENVIRONMENT, LOC_OTHER,
)

#: String-table key -> location, for the named inventory components on the pawn.
_PLAYER_COMPONENT_LABELS = {
    "Backpack": LOC_BACKPACK,
    "Holster": LOC_HOLSTER,
    "Equipment": LOC_EQUIPPED,
}


def is_carried(location: str) -> bool:
    """True when a record lives in a component the player can actually use."""
    return location in CARRIED_LOCATIONS


def classify_item_location(actor: str) -> str:
    folded = actor.casefold()
    if "firstpersoncharacter" in folded or "playercharacter" in folded:
        return LOC_BACKPACK
    if "playerbox" in folded:
        return LOC_PLAYER_CRATE
    if "inventory_container" in folded:
        return LOC_CONTAINER
    if (
        "dynamicmeshactor" in folded
        or "dynamicskeletalmeshactor" in folded
        or "glowstick" in folded
    ):
        return LOC_ENVIRONMENT
    return LOC_OTHER


def is_player_map(item_map: "voyage_gvas.ItemMap") -> bool:
    """True for an inventory map serialized by the player's own actor.

    The manual save slot files the pawn under ``pawns``, but the game's
    AutoSave files it under whichever world-partition cell the player is
    standing in (for example ``119:43``), so the owner key alone is not
    enough; the persistent actor that wrote the map is the stable identity.
    """
    return (
        item_map.owner == PLAYER_OWNER
        or classify_item_location(item_map.actor) == LOC_BACKPACK
    )


def player_component_location(item_map: "voyage_gvas.ItemMap") -> str:
    """Map one of the pawn's inventory components to a display location.

    The player actor carries eight ``Items`` maps but only three of them are
    real containers.  The named ones (``Backpack``/``Holster``/``Equipment``)
    are what the in-game UI shows; the unnamed ones hold the recipe/unlock
    catalog and a module-side mirror of the backpack, which is why items such
    as ``LightLancer HeadLight`` appear in the save yet never in the pack.
    """
    if item_map.label in _PLAYER_COMPONENT_LABELS:
        return _PLAYER_COMPONENT_LABELS[item_map.label]
    if item_map.is_module:
        return LOC_MODULE
    return LOC_CATALOG


def component_ranges(raw: bytes) -> list[tuple[int, int, str]]:
    """Return sorted ``(start, end, location)`` spans for the pawn's components."""
    spans: list[tuple[int, int, str]] = []
    for item_map in voyage_gvas.find_item_maps(raw):
        if not is_player_map(item_map) or not item_map.entries:
            continue
        spans.append((
            item_map.tag.start,
            item_map.tag.end,
            player_component_location(item_map),
        ))
    spans.sort()
    return spans


def find_mirrors(raw: bytes) -> list[tuple[int, int, int]]:
    """Find player inventory maps the save stores twice, byte for byte.

    The pawn serializes its backpack both as a plain inventory component and
    again inside a module component, and the two payloads are identical. An
    edit applied to only one copy leaves the save internally inconsistent, so
    :func:`apply_edits` mirrors every change across the pair.

    Returns ``(start, end, delta)`` triples covering both directions, where
    ``offset + delta`` is the twin of any offset inside ``[start, end)``.
    """
    maps = [
        item_map for item_map in voyage_gvas.find_item_maps(raw)
        if is_player_map(item_map) and item_map.entries
    ]
    mirrors: list[tuple[int, int, int]] = []
    paired: set[int] = set()
    for index, left in enumerate(maps):
        if left.tag.start in paired:
            continue
        for right in maps[index + 1:]:
            if right.tag.start in paired or right.tag.size != left.tag.size:
                continue
            if raw[left.tag.payload:left.tag.end] != raw[right.tag.payload:right.tag.end]:
                continue
            delta = right.tag.payload - left.tag.payload
            mirrors.append((left.tag.payload, left.tag.end, delta))
            mirrors.append((right.tag.payload, right.tag.end, -delta))
            paired.add(left.tag.start)
            paired.add(right.tag.start)
            break
    mirrors.sort()
    return mirrors


def mirror_offset(mirrors: list[tuple[int, int, int]], offset: int) -> int | None:
    """Return the twin of ``offset``, or ``None`` when it is not mirrored."""
    if not mirrors:
        return None
    index = bisect.bisect_right([span[0] for span in mirrors], offset) - 1
    if index < 0:
        return None
    start, end, delta = mirrors[index]
    if start <= offset < end:
        return offset + delta
    return None


def refine_locations(items: list[InventoryItem], spans: list[tuple[int, int, str]]) -> None:
    """Replace the coarse actor-based location with the owning component."""
    if not spans:
        return
    starts = [span[0] for span in spans]
    for item in items:
        index = bisect.bisect_right(starts, item.anchor_offset) - 1
        if index < 0:
            continue
        start, end, location = spans[index]
        if start <= item.anchor_offset < end:
            item.location = location


def attach_context(entries: list[Entry], anchors: list[TextAnchor]) -> None:
    relevant = [
        anchor for anchor in anchors
        if anchor.field in {
            "ActorName", "AssetName", "PrimaryAssetName", "Name", "DebugName",
            "ActorClass", "NPCData", "SlotName",
        }
    ]
    anchor_index = 0
    recent: dict[str, TextAnchor] = {}
    for entry in entries:
        while anchor_index < len(relevant) and relevant[anchor_index].offset < entry.offset:
            anchor = relevant[anchor_index]
            if anchor.field == "Name" and anchor.value in {
                "Item", "Material", "Equipment", "Skill", "Prop"
            }:
                recent["Category"] = anchor
            else:
                recent[anchor.field] = anchor
            anchor_index += 1
        parts: list[str] = []
        for key in ("ActorName", "Category", "AssetName", "PrimaryAssetName", "DebugName"):
            anchor = recent.get(key)
            if not anchor or entry.offset - anchor.offset > 100_000:
                continue
            value = _short_asset(anchor.value) if "AssetName" in key else anchor.value
            if key == "ActorName":
                value = value.split("_UAID_", 1)[0]
            parts.append(f"{key}={value}")
        entry.context = " | ".join(parts[-4:])


def group_inventory(entries: list[Entry], anchors: list[TextAnchor]) -> list[InventoryItem]:
    asset_anchors = [anchor for anchor in anchors if anchor.field == "AssetName"]
    if not asset_anchors:
        return []
    entry_cursor = 0
    items: list[InventoryItem] = []
    recent_category = ""
    recent_actor = ""
    anchor_cursor = 0
    for index, asset_anchor in enumerate(asset_anchors):
        while anchor_cursor < len(anchors) and anchors[anchor_cursor].offset <= asset_anchor.offset:
            anchor = anchors[anchor_cursor]
            if anchor.field == "Name" and anchor.value in {
                "Item", "Material", "Equipment", "Skill", "Prop"
            }:
                recent_category = anchor.value
            elif anchor.field == "ActorName":
                recent_actor = anchor.value.split("_UAID_", 1)[0]
            anchor_cursor += 1
        next_offset = (
            asset_anchors[index + 1].offset
            if index + 1 < len(asset_anchors)
            else (entries[-1].offset + 1 if entries else asset_anchor.offset + 1)
        )
        while entry_cursor < len(entries) and entries[entry_cursor].offset < asset_anchor.offset:
            entry_cursor += 1
        item_entries: dict[str, Entry] = {}
        cursor = entry_cursor
        while cursor < len(entries) and entries[cursor].offset < next_offset:
            entry = entries[cursor]
            if entry.name in {
                "ItemCount", "Health", "Mass", "SelectedVariation",
                "PrimaryIconIndex", "SecondaryIconIndex", "ExtraDamage",
            } and entry.name not in item_entries:
                item_entries[entry.name] = entry
            cursor += 1
        if "ItemCount" in item_entries:
            items.append(InventoryItem(
                len(items),
                _short_asset(asset_anchor.value),
                recent_category,
                recent_actor,
                classify_item_location(recent_actor),
                asset_anchor.offset,
                item_entries,
            ))
    return items


def scan_resource_amounts(
    raw: bytes,
    anchors: list[TextAnchor],
    items: list[InventoryItem],
    item_maps: Iterable[voyage_gvas.ItemMap] = (),
) -> list[ResourceAmount]:
    """Decode untagged quantities in Resources<EModuleResourceType, double> maps."""
    actors = sorted(
        (anchor for anchor in anchors if anchor.field == "ActorName"),
        key=lambda anchor: anchor.offset,
    )
    actor_offsets = [anchor.offset for anchor in actors]
    sorted_items = sorted(items, key=lambda item: item.anchor_offset)
    item_offsets = [item.anchor_offset for item in sorted_items]
    spans = []
    for item_map in item_maps:
        for map_entry in item_map.entries:
            index = bisect.bisect_left(item_offsets, map_entry.start)
            if index < len(sorted_items) and item_offsets[index] < map_entry.end:
                spans.append((map_entry.start, map_entry.end, sorted_items[index]))
    spans.sort(key=lambda span: span[0])
    span_starts = [span[0] for span in spans]
    needle = _fstring_tag("Resources")
    start = 0
    result: list[ResourceAmount] = []
    while True:
        offset = raw.find(needle, start)
        if offset < 0:
            break
        start = offset + len(needle)
        try:
            tag, _ = voyage_gvas.read_tag(raw, offset)
        except voyage_gvas.GvasError:
            continue
        if tag is None or tag.type_name != "MapProperty" or len(tag.type.children) != 2:
            continue
        key_type, value_type = tag.type.children
        if (
            key_type.name != "EnumProperty"
            or not key_type.children
            or key_type.children[0].name != "EModuleResourceType"
            or value_type.name not in {"FloatProperty", "DoubleProperty"}
        ):
            continue
        try:
            _, map_entries = voyage_gvas.parse_map(raw, tag)
        except (voyage_gvas.GvasError, struct.error) as exc:
            raise SaveFormatError(f"Invalid Resources map at 0x{offset:X}: {exc}") from exc
        actor_index = bisect.bisect_right(actor_offsets, offset) - 1
        actor = actors[actor_index].value if actor_index >= 0 else ""
        asset = ""
        location = classify_item_location(actor)
        span_index = bisect.bisect_right(span_starts, offset) - 1
        if span_index >= 0 and tag.end <= spans[span_index][1]:
            item = spans[span_index][2]
            actor, asset, location = item.actor, item.asset, item.location
        for map_entry in map_entries:
            if not isinstance(map_entry.key, str) or not map_entry.key.startswith("EModuleResourceType::"):
                raise SaveFormatError(f"Invalid resource key at 0x{map_entry.start:X}")
            resource = map_entry.key.split("::", 1)[1]
            value, _ = voyage_gvas.read_untagged(raw, map_entry.value_start, value_type)
            entry = Entry(
                0, map_entry.value_start, f"Resources.{resource}", value_type.name, value,
                f"ActorName={actor} | AssetName={asset} | Location={location} | Resources=0x{offset:X}",
            )
            result.append(ResourceAmount(resource, actor, asset, location, offset, entry))
    return result


def is_refillable_storage(record: ResourceAmount) -> bool:
    """Resources shown in the editor except the player's infinite backpack electricity."""
    actor = record.actor.casefold()
    return "_c_uaid_" not in actor and "npc" not in actor and not (
        record.resource.casefold() == "electricity"
        and record.actor.casefold() == "bp_firstpersoncharacter_new_c1"
        and record.location == LOC_BACKPACK
        and math.isinf(float(record.entry.value))
    )


def storage_refill_edits(
    document: SaveDocument, target: int, health: int = 10000, extra_damage: int = 10000,
) -> list[tuple[Entry, int]]:
    for label, value in (("amount", target), ("health", health), ("extra damage", extra_damage)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_STACK_TARGET:
            raise ValueError(f"Storage {label} must be between 1 and {MAX_STACK_TARGET}")
    protected_offsets = {
        entry.offset
        for record in document.resources if not is_refillable_storage(record)
        for entry in (record.entry, record.health, record.extra_damage) if entry is not None
    }
    edits: dict[int, tuple[Entry, int]] = {}
    for record in document.resources:
        if not is_refillable_storage(record):
            continue
        for entry, value in ((record.entry, target), (record.health, health), (record.extra_damage, extra_damage)):
            if entry is not None and entry.offset not in protected_offsets:
                edits[entry.offset] = (entry, value)
    return list(edits.values())


def attach_resource_health(
    resources: list[ResourceAmount], entries: list[Entry],
    anchors: list[TextAnchor], items: list[InventoryItem],
) -> None:
    """Associate only unambiguous health fields within the same actor or inventory item."""
    actor_offsets = sorted(a.offset for a in anchors if a.field == "ActorName")
    fields: dict[tuple[int, str], list[Entry]] = {}
    for entry in entries:
        if entry.name in {"Health", "ExtraDamage"}:
            index = bisect.bisect_right(actor_offsets, entry.offset) - 1
            fields.setdefault((index, entry.name), []).append(entry)
    item_health: dict[tuple[str, str, str], list[InventoryItem]] = {}
    for item in items:
        item_health.setdefault((item.actor, item.asset, item.location), []).append(item)
    for record in resources:
        if record.asset:
            matches = item_health.get((record.actor, record.asset, record.location), [])
            if len(matches) == 1:
                item = matches[0]
                record.health = item.entries.get("Health")
                record.extra_damage = item.entries.get("ExtraDamage")
            continue
        index = bisect.bisect_right(actor_offsets, record.map_offset) - 1
        for name, attribute in (("Health", "health"), ("ExtraDamage", "extra_damage")):
            candidates = fields.get((index, name), []) if index >= 0 else []
            if len(candidates) == 1:
                setattr(record, attribute, candidates[0])


def player_stat_entries(document: SaveDocument) -> list[tuple[str, Entry]]:
    """Return scalar fields from named stat blocks on the player actor."""
    actor_anchors = [anchor for anchor in document.anchors if anchor.field == "ActorName"]
    player_index = next(
        (
            index for index, anchor in enumerate(actor_anchors)
            if "FirstPersonCharacter" in anchor.value
        ),
        None,
    )
    if player_index is None:
        return []
    start = actor_anchors[player_index].offset
    end = (
        actor_anchors[player_index + 1].offset
        if player_index + 1 < len(actor_anchors)
        else len(document.raw)
    )
    debug_anchors = [
        anchor for anchor in document.anchors
        if anchor.field == "DebugName" and start < anchor.offset < end
    ]
    result: list[tuple[str, Entry]] = []
    block_start = start
    for debug in debug_anchors:
        fields: dict[str, Entry] = {}
        for entry in document.entries:
            if block_start < entry.offset < debug.offset and entry.name in {
                "Value", "MinValue", "MaxValue", "DefaultValue",
            }:
                fields[entry.name] = entry
        for field, suffix in (
            ("Value", "Current"),
            ("MinValue", "Minimum"),
            ("MaxValue", "Maximum"),
            ("DefaultValue", "Default"),
        ):
            if field in fields:
                result.append((f"{debug.value} - {suffix}", fields[field]))
        block_start = debug.offset
    if not result and _SURVIVAL_TAG in document.raw[start:end]:
        for attribute, fields in player_survival_fields(document.raw).items():
            for name, suffix in (
                ("Value", "Current"), ("MinValue", "Minimum"),
                ("MaxValue", "Maximum"), ("DefaultValue", "Default"),
            ):
                if name in fields:
                    result.append((f"{attribute} - {suffix}", fields[name]))
    return result


def enhancer_points_entries(document: SaveDocument) -> list[tuple[str, Entry]]:
    """Return each 'Enhancer' (a.k.a. Scaler) Points value on the player.

    These are the caretaker's spendable perk points -- e.g. ``Enhancer
    Character HaulMastery`` governs carry weight. The save stores one
    ``Points`` IntProperty per enhancer on the ``PecoLockStates`` actor,
    distinguished only by the ``PrimaryAssetName`` anchor that precedes it.
    """
    result: list[tuple[str, Entry]] = []
    for entry in document.entries:
        if entry.name != "Points" or entry.type != "IntProperty":
            continue
        marker = "PrimaryAssetName=Enhancer "
        idx = entry.context.find(marker)
        if idx == -1:
            continue
        label = entry.context[idx + len(marker):].split(" | ", 1)[0].strip()
        if label:
            result.append((label, entry))
    return result


# Enhancers that drive health and energy capacity. Game v6 stops serializing
# the player's survival Min/Max, so these points are the save-side lever.
SURVIVAL_ENHANCERS = (
    "Character Vitality",
    "Character Battery",
    "Battery",
    "Character Power Management",
)
SURVIVAL_ENHANCER_BOOST = 999999


def survival_enhancer_entries(document: SaveDocument) -> list[tuple[str, Entry]]:
    """Return the Vitality/Battery/Power Management enhancer Points entries."""
    return [(label, entry) for label, entry in enhancer_points_entries(document) if label in SURVIVAL_ENHANCERS]


# Player SurvivalAttributes that a trainer can pin so they never drop.
# ``Level`` is the caretaker's energy/power meter (distinct from the integer
# caretaker ``Level`` progression field). Normal values come from an untouched
# save; the locked values are exactly what the trainer left behind.
PLAYER_LOCKS: dict[str, dict] = {
    "health": {
        "attribute": "Health",
        "label": "Infinite Health",
        "normal": {"MinValue": 0.0, "MaxValue": 200.0, "DefaultValue": 100.0, "Value": 200.0},
        "locked": {"MinValue": 200.0, "MaxValue": 200.0, "DefaultValue": 200.0, "Value": 200.0},
    },
    "energy": {
        "attribute": "Level",
        "label": "Infinite Energy",
        "normal": {"MinValue": 0.0, "MaxValue": 100.0, "DefaultValue": 1.0, "Value": 100.0},
        "locked": {"MinValue": 200.0, "MaxValue": 100.0, "DefaultValue": 200.0, "Value": 200.0},
    },
}
_SURVIVAL_TAG = b"\x13\x00\x00\x00SurvivalAttributes\x00"
_LOCK_FIELDS = ("Value", "MinValue", "MaxValue", "DefaultValue")


def player_survival_fields(raw: bytes) -> dict[str, dict[str, Entry]]:
    """``{attribute: {field: Entry}}`` for the player's SurvivalAttributes map.

    The player map is the only one carrying both a ``Health`` and a ``Level``
    attribute; NPC/creature maps hold Health alone. Newer saves may serialize
    only ``Value``, without editable limits or debug labels.
    """
    found: list[dict[str, dict[str, Entry]]] = []
    for match in re.finditer(re.escape(_SURVIVAL_TAG), raw):
        try:
            tag, _ = voyage_gvas.read_tag(raw, match.start())
            if tag is None or tag.type_name != "MapProperty":
                continue
            _num_remove, entries = voyage_gvas.parse_map(raw, tag)
        except (voyage_gvas.GvasError, struct.error):
            continue
        attributes: dict[str, dict[str, Entry]] = {}
        for map_entry in entries:
            name = str(map_entry.key).split("::")[-1]
            offsets = voyage_gvas.entry_field_offsets(raw, map_entry)
            values = voyage_gvas.entry_values(raw, map_entry)
            fields = {}
            for field_name in _LOCK_FIELDS:
                if field_name in offsets and isinstance(values.get(field_name), float):
                    fields[field_name] = Entry(
                        -1, offsets[field_name], field_name, "FloatProperty",
                        values[field_name], f"Player SurvivalAttributes {name}")
            if "Value" in fields:
                attributes[name] = fields
        if "Health" in attributes and "Level" in attributes:
            found.append(attributes)
    if len(found) != 1:
        raise SaveFormatError(f"Expected one player SurvivalAttributes map, found {len(found)}")
    return found[0]


def player_lock_states(document: SaveDocument) -> dict[str, bool]:
    """True when the attribute is pinned (its minimum can't drop below current)."""
    fields = player_survival_fields(document.raw)
    states = {}
    for key, spec in PLAYER_LOCKS.items():
        row = fields[spec["attribute"]]
        if not all(name in row for name in _LOCK_FIELDS):
            continue
        states[key] = float(row["MinValue"].value) >= min(
            float(row["MaxValue"].value), float(row["Value"].value)) > 0
    return states


def player_lock_edits(document: SaveDocument, key: str, locked: bool) -> list[tuple[Entry, float]]:
    spec = PLAYER_LOCKS[key]
    row = player_survival_fields(document.raw)[spec["attribute"]]
    if not all(name in row for name in _LOCK_FIELDS):
        raise SaveFormatError(
            f"This save does not serialize the {spec['attribute']} limits needed "
            "for an infinite survival lock. Only its current value can be edited."
        )
    wanted = spec["locked" if locked else "normal"]
    return [
        (row[name], value) for name, value in wanted.items()
        if float(row[name].value) != value
    ]


def set_player_lock(document: SaveDocument, key: str, locked: bool, *,
                    make_backup: bool = True, max_backups: int = 20) -> tuple[int, Path | None]:
    """Toggle infinite health/energy by rewriting the attribute's floats in place."""
    return apply_edits(document, player_lock_edits(document, key, locked),
                       make_backup=make_backup, max_backups=max_backups)


DEFAULT_STACK = 999
MODULE_STACK = 5
PAINT_STACK = 10
AMMO_REPAIR_STACK = 2500
BIG_PETROL_GRENADE_REPAIR_STACK = 2500
GRENADE_REPAIR_STACK = 500
WEAPON_ATTACHMENT_REPAIR_STACK = 10
CABLE_REPAIR_STACK = 100
CYLINDER_REPAIR_STACK = 50
GLOW_REPAIR_STACK = 100
CARTRIDGE_STACK = 10
SINGLE_STACK = 1
DEFAULT_FULL_HEALTH = 100.0
_SINGLE_PREFIXES = ("back", "cartridge", "prop", "memory", "itemloot")
_ALWAYS_BULK_PREFIXES = ("part", "cable", "ammo")
_DUPLICATE_PROTECTED_CATEGORIES = frozenset({"ammo", "memory", "weapon"})
_ALWAYS_REMOVE_DUPLICATES = frozenset({
    "weapon grenadepetrol",
    "weapon grenadeoxygen",
    "weapon grenadeelectric",
})


def _is_grenade(asset: str) -> bool:
    """Grenades are Weapon assets, but duplicate grenade stacks are always combined."""
    folded = asset.strip().casefold()
    return folded.startswith("weapon") and "grenade" in folded


_SINGLE_KEYWORDS = ("waterjet", "headlight", "flashlight", "headlamp", "dismantletool", "repairtool", "seamine")
_SINGLE_OVERRIDES = ("binoculars", "scantool")
_MODULE_OVERRIDES = ("mooringsocket",)
_CYLINDER_EXCEPTIONS = ("partstoragecylinder",)
_GLOW_KEYWORDS = ("glow",)


def normalize_deletion_items(names: object) -> list[str]:
    """Unique exact inventory names, persisted independently of stack targets."""
    if not isinstance(names, list):
        return []
    unique = {}
    for name in names:
        if isinstance(name, str) and name.strip():
            unique.setdefault(name.strip().casefold(), name.strip())
    return sorted(unique.values(), key=str.casefold)


def deletion_records(document: SaveDocument, names: list[str]) -> list[InventoryItem]:
    """Configured carried records, excluding weapon attachments and mirrors."""
    wanted = {name.casefold() for name in normalize_deletion_items(names)}
    if not wanted:
        return []
    installed = installed_attachment_offsets(document.raw)
    result: list[InventoryItem] = []
    seen: set[int] = set()
    for item in document.items:
        if not is_carried(item.location) or item.asset.strip().casefold() not in wanted:
            continue
        if _is_weapon_attachment(item.asset, item.category):
            continue
        entry = item.entries.get("ItemCount")
        if entry is None or entry.offset in installed or entry.offset in seen:
            continue
        seen.add(entry.offset)
        result.append(item)
    return result


def is_bulk_resource(asset: str, category: str = "") -> bool:
    """True for items that always stack to 999 (materials, parts, cables, ammo).

    These are the categories most likely to carry real per-unit physical
    weight, so they are the first place to look when total carried weight
    is too high -- the save has no per-item weight field to check directly.
    """
    name = asset.strip()
    folded = name.casefold()
    return category.casefold() == "material" or folded.startswith(_ALWAYS_BULK_PREFIXES)


def _is_weapon_attachment(asset: str, category: str = "") -> bool:
    folded = asset.strip().casefold()
    return (
        category.strip().casefold() == "weapon attachment"
        or folded.startswith("weapon") and "attachment" in folded
    )


REPAIR_CATEGORIES: tuple[tuple[str, tuple[str, ...], int, bool], ...] = (
    # key, label path (category > subcategory...), default target, applies to non-carried records
    ("weapons.guns", ("Weapons", "Guns & Tools"), SINGLE_STACK, False),
    ("weapons.grenades.big_petrol", ("Weapons", "Grenades", "Big Petrol"), BIG_PETROL_GRENADE_REPAIR_STACK, True),
    ("weapons.grenades.other", ("Weapons", "Grenades", "Other Grenades"), GRENADE_REPAIR_STACK, True),
    ("weapons.attachments", ("Weapons", "Attachments (loose)"), WEAPON_ATTACHMENT_REPAIR_STACK, True),
    ("ammo", ("Ammo",), AMMO_REPAIR_STACK, True),
    ("memory", ("Memory",), 10, True),
    ("food", ("Food",), DEFAULT_STACK, False),
    ("materials", ("Resources", "Materials"), DEFAULT_STACK, False),
    ("parts", ("Resources", "Parts"), DEFAULT_STACK, False),
    ("cables", ("Resources", "Cables"), CABLE_REPAIR_STACK, False),
    ("resources.scraps", ("Resources", "Scraps"), DEFAULT_STACK, False),
    ("resources.nuggets", ("Resources", "Ore Nuggets"), DEFAULT_STACK, False),
    ("resources.pigments", ("Resources", "Pigments"), DEFAULT_STACK, False),
    ("resources.bio", ("Resources", "Bio Matter"), DEFAULT_STACK, False),
    ("consumables", ("Consumables",), DEFAULT_STACK, False),
    ("consumables.charge_injector", ("Consumable ChargeInjector",), 500, False),
    ("modules.modules", ("Modules", "Modules & Sockets"), MODULE_STACK, False),
    ("modules.enhancers", ("Modules", "Enhancers"), MODULE_STACK, False),
    ("cylinders", ("Modules", "Cylinders (engines & boosters)"), CYLINDER_REPAIR_STACK, False),
    ("paint.paint", ("Paint", "Paint (exact name)"), PAINT_STACK, False),
    ("paint.designs", ("Paint", "Paint Designs"), MODULE_STACK, False),
    ("equipment.back", ("Equipment", "Back Batteries & Tanks"), SINGLE_STACK, False),
    ("equipment.tools", ("Equipment", "Tools & Lights"), SINGLE_STACK, False),
    ("equipment.glow", ("Equipment", "Glow Sticks & Depth Glow"), GLOW_REPAIR_STACK, False),
    ("cartridges", ("Cartridges",), CARTRIDGE_STACK, True),
    ("props", ("Props & Loot",), SINGLE_STACK, False),
    ("other", ("Everything Else",), DEFAULT_STACK, False),
    ("not_carried", ("Not Carried (non-special items)",), SINGLE_STACK, False),
    ("storage.tanks", ("Storage", "Stored Amount"), 50000, False),
    ("storage.health", ("Storage", "Health (raw)"), 10000, False),
    ("storage.extra_damage", ("Storage", "Extra Damage (raw)"), 10000, False),
)
REPAIR_CATEGORY_KEYS = tuple(row[0] for row in REPAIR_CATEGORIES)
_REPAIR_EVERYWHERE = frozenset(row[0] for row in REPAIR_CATEGORIES if row[3])
MAX_STACK_TARGET = 2**31 - 1


def default_repair_targets() -> dict[str, int]:
    return {key: default for key, _label, default, _everywhere in REPAIR_CATEGORIES}


def normalize_repair_targets(targets: dict | None) -> dict[str, int]:
    """Defaults overlaid with every valid ``{category key: positive int}`` override."""
    result = default_repair_targets()
    if not isinstance(targets, dict):
        return result
    for key, value in targets.items():
        if key not in result or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= MAX_STACK_TARGET:
            result[key] = number
    return result


def normalize_asset_targets(targets: dict | None) -> dict[str, int]:
    """Validate a ``{asset name: stack size}`` override map, dropping bad rows."""
    result: dict[str, int] = {}
    if not isinstance(targets, dict):
        return result
    for key, value in targets.items():
        if not isinstance(key, str) or isinstance(value, bool):
            continue
        name = key.strip()
        if not name:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= MAX_STACK_TARGET:
            result[name] = number
    return dict(sorted(result.items(), key=lambda row: row[0].casefold()))


def _asset_target_lookup(asset_targets: dict | None) -> dict[str, int]:
    return {
        name.casefold(): value
        for name, value in normalize_asset_targets(asset_targets).items()
    }


def asset_targets_from_document(document: SaveDocument) -> dict[str, int]:
    """Per-item targets matching what the player is carrying right now.

    Each carried asset maps to the largest non-zero stack currently held, so
    replaying it through Repair & Top Off reproduces today's inventory exactly.
    Attachments installed inside a weapon are skipped: their count is never
    rewritten.
    """
    installed = installed_attachment_offsets(document.raw)
    captured: dict[str, int] = {}
    for item in document.items:
        if not is_carried(item.location):
            continue
        entry = item.entries.get("ItemCount")
        if entry is None or entry.offset in installed:
            continue
        count = int(entry.value)
        if not 1 <= count <= MAX_STACK_TARGET:
            continue
        name = item.asset.strip()
        if name and count > captured.get(name, 0):
            captured[name] = count
    return dict(sorted(captured.items(), key=lambda row: row[0].casefold()))


def repair_category(asset: str, category: str = "") -> str:
    """Classify an item into one of ``REPAIR_CATEGORY_KEYS`` (never ``not_carried``)."""
    folded_category = category.strip().casefold()
    folded = asset.strip().casefold()
    compact = folded.replace(" ", "").replace("_", "")
    if folded == "consumable chargeinjector":
        return "consumables.charge_injector"
    if _is_weapon_attachment(asset, category):
        return "weapons.attachments"
    if folded.startswith("cartridge"):
        return "cartridges"
    if folded_category == "memory" or folded.startswith("memory"):
        return "memory"
    if folded_category == "ammo" or folded.startswith("ammo"):
        return "ammo"
    if folded_category == "food" or folded == "food" or folded.startswith(("food ", "food_")):
        return "food"
    if folded == "nightvision":
        return "equipment.tools"
    if "grenade" in folded:
        if "petrol" in folded and "big" in folded:
            return "weapons.grenades.big_petrol"
        return "weapons.grenades.other"
    if compact in _SINGLE_OVERRIDES:
        return "equipment.tools"
    if folded == "paint":
        return "paint.paint"
    if compact in _MODULE_OVERRIDES:
        return "modules.modules"
    if folded.startswith("paint"):
        return "paint.designs"
    if "cylinder" in folded and compact not in _CYLINDER_EXCEPTIONS:
        return "cylinders"
    if any(keyword in compact for keyword in _GLOW_KEYWORDS):
        return "equipment.glow"
    if folded.startswith("cable") or "cable" in folded.split():
        return "cables"
    if folded_category == "material":
        return "materials"
    if folded.startswith("part"):
        return "parts"
    if folded.startswith("back"):
        return "equipment.back"
    if folded.startswith(("prop", "itemloot")):
        return "props"
    if any(keyword in compact for keyword in _SINGLE_KEYWORDS):
        return "equipment.tools"
    if folded.startswith("weapon"):
        return "weapons.guns"
    if folded.startswith("module"):
        return "modules.enhancers" if "enhancer" in folded else "modules.modules"
    words = folded.split()
    if words and words[-1] in ("scrap", "scraps"):
        return "resources.scraps"
    if words and words[-1] in ("nugget", "nuggets"):
        return "resources.nuggets"
    if folded.startswith("pigment"):
        return "resources.pigments"
    if words and words[0] == "bio":
        return "resources.bio"
    if folded.startswith("consumable"):
        return "consumables"
    return "other"


def repair_stack_target(asset: str, category: str, carried: bool,
                        targets: dict | None = None,
                        asset_targets: dict | None = None) -> int:
    """Stack size Repair & Top Off should refill to.

    ``asset_targets`` maps an exact item name to the stack size that item
    should always end up at; it wins over every category rule. ``targets``
    maps ``REPAIR_CATEGORY_KEYS`` to configured stack sizes (defaults fill any
    gap). Weapon attachments, Memory, Ammo, grenades, and Cartridges use their
    category target even when not carried; any other record the player is not
    carrying uses the ``not_carried`` target (1 by default).
    """
    configured = normalize_repair_targets(targets)
    key = repair_category(asset, category)
    if not carried and key not in _REPAIR_EVERYWHERE:
        return configured["not_carried"]
    override = _asset_target_lookup(asset_targets).get(asset.strip().casefold())
    if override is not None:
        return override
    return configured[key]


def stack_target(asset: str, category: str = "") -> int:
    """Return the sensible maximum stack size for an item.

    Bulk resources always top off at 999, weapons/tools/unique props stay at 1,
    deployable modules and boosters cap at 5, and anything else defaults to 999.
    """
    name = asset.strip()
    folded = name.casefold()
    compact = folded.replace(" ", "").replace("_", "")
    if compact in _SINGLE_OVERRIDES:
        return SINGLE_STACK
    if folded == "paint":
        return PAINT_STACK
    if compact in _MODULE_OVERRIDES or folded.startswith("paint"):
        return MODULE_STACK
    if "cylinder" in folded and compact not in _CYLINDER_EXCEPTIONS:
        return CYLINDER_REPAIR_STACK
    if any(keyword in compact for keyword in _GLOW_KEYWORDS):
        return GLOW_REPAIR_STACK
    if folded.startswith("cartridge"):
        return CARTRIDGE_STACK
    if category.casefold() == "material" or folded.startswith(_ALWAYS_BULK_PREFIXES):
        return DEFAULT_STACK
    if folded.startswith(_SINGLE_PREFIXES):
        return SINGLE_STACK
    if any(keyword in compact for keyword in _SINGLE_KEYWORDS):
        return SINGLE_STACK
    if _is_weapon_attachment(asset):
        return MODULE_STACK
    if folded.startswith("weapon") and "grenade" not in folded:
        return SINGLE_STACK
    if folded.startswith("module"):
        return MODULE_STACK
    return DEFAULT_STACK


def full_health_target(document: SaveDocument, item: InventoryItem) -> float | None:
    """Best-known undamaged health for an item, derived from the save itself."""
    entry = item.entries.get("Health")
    if entry is None:
        return None
    current = float(entry.value)
    best = max(
        (
            float(other.entries["Health"].value)
            for other in document.items
            if other.asset == item.asset and "Health" in other.entries
        ),
        default=current,
    )
    if best > current:
        return best
    return max(current, DEFAULT_FULL_HEALTH)


def installed_attachment_offsets(raw: bytes) -> set[int]:
    """``ItemCount`` offsets of attachments installed inside a weapon.

    These live in a weapon's own ``Attachments`` map. Their count is left
    exactly as the game wrote it: rewriting it makes the game eject the
    attachment from its slot, after which it is lost.
    """
    try:
        maps = voyage_gvas.find_item_maps(raw, name="Attachments")
    except (voyage_gvas.GvasError, struct.error, ValueError):
        return set()
    offsets: set[int] = set()
    for item_map in maps:
        for entry in item_map.entries:
            offset = voyage_gvas.entry_field_offsets(raw, entry).get("Data.ItemCount")
            if offset is not None:
                offsets.add(offset)
    return offsets


def repair_and_top_off(document: SaveDocument, items: list[InventoryItem],
                       targets: dict | None = None,
                       asset_targets: dict | None = None) -> list[tuple[Entry, int | float]]:
    """Compute count/health edits that refill and repair the given records.

    Carried records (backpack/holster/equipped) are topped off using
    `repair_stack_target`: a per-item override in ``asset_targets`` wins,
    otherwise the category target applies (materials, parts, cables top off at
    999; ammo, grenades, and attachments use their Repair & Top
    Off-specific targets). Records the player is not
    carrying (environment, containers, catalog, module mirror) are compacted
    to a stack of 1 instead of being topped off, except for those special
    targets. Zero-count records are never revived.
    """
    edits: list[tuple[Entry, int | float]] = []
    configured = normalize_repair_targets(targets)
    overrides = normalize_asset_targets(asset_targets)
    installed = installed_attachment_offsets(document.raw)
    for item in items:
        count_entry = item.entries.get("ItemCount")
        if (
            count_entry is not None
            and int(count_entry.value) > 0
            and count_entry.offset not in installed
        ):
            target = repair_stack_target(
                item.asset, item.category, is_carried(item.location), configured, overrides)
            if int(count_entry.value) != target:
                edits.append((count_entry, target))
        health_entry = item.entries.get("Health")
        if health_entry is not None:
            target_health = full_health_target(document, item)
            if target_health is not None and float(health_entry.value) != target_health:
                edits.append((health_entry, target_health))
    return edits


def human_seed_time_edits(document: SaveDocument) -> list[tuple[Entry, float]]:
    """Extend HumanSeed timers using their serialized actor ownership."""
    actors = sorted(
        (anchor for anchor in document.anchors if anchor.field == "ActorName"),
        key=lambda anchor: anchor.offset,
    )
    offsets = [actor.offset for actor in actors]
    edits: list[tuple[Entry, float]] = []
    for entry in document.entries:
        if entry.name != "TimeLeft" or entry.type != "FloatProperty":
            continue
        index = bisect.bisect_right(offsets, entry.offset) - 1
        if index >= 0 and actors[index].value.startswith("BP_Item_HumanSeed_"):
            if entry.value != 99999.0:
                edits.append((entry, 99999.0))
    return edits


ROCKET_ACTOR_PREFIX = "BP_Rocket_Functional_C"
ROCKET_RESOURCE_TARGETS = {"Electricity": 60000, "Methane": 3000, "Oxygen": 12000}


def rocket_resource_edits(document: SaveDocument) -> list[tuple[Entry, int]]:
    """Fill the hidden Functional Rocket's Electricity, Methane and Oxygen amounts."""
    prefix = ROCKET_ACTOR_PREFIX.casefold()
    targets = {name.casefold(): value for name, value in ROCKET_RESOURCE_TARGETS.items()}
    edits: dict[int, tuple[Entry, int]] = {}
    for record in document.resources:
        target = targets.get(record.resource.casefold())
        if target is None or not record.actor.casefold().startswith(prefix):
            continue
        if float(record.entry.value) != target:
            edits[record.entry.offset] = (record.entry, target)
    return list(edits.values())


def player_item_catalog(document: SaveDocument) -> dict[str, list[InventoryItem]]:
    """Return every serialized player item template grouped by display name."""
    catalog: dict[str, list[InventoryItem]] = {}
    for item in document.items:
        if is_carried(item.location):
            catalog.setdefault(item.asset, []).append(item)
    return dict(sorted(catalog.items(), key=lambda row: row[0].casefold()))


def find_duplicate_stacks(
    document: SaveDocument, *, include_memory: bool = False,
) -> list[InventoryItem]:
    """Return carried stacks that are redundant duplicates of the same item.

    Stackable items (materials, parts, ammo, etc.) can end up split across
    several separate records of the same asset -- e.g. two records of ``Iron``
    both sitting at a 999 stack, instead of one. This keeps the single
    largest carried stack of each distinct item (same asset and, when
    present, the same ``SelectedVariation``) and returns every other stack of
    it, so callers can purge them and leave exactly one stack behind. This
    applies regardless of the item's normal stack size -- single-count
    non-weapon tools and weapon attachments are covered
    exactly like 999-count materials. Ammo, Memory, and Weapon categories are
    protected from normal duplicate removal, except grenades (any ``Weapon``
    asset containing ``Grenade``), whose duplicate stacks are always removed;
    three exact player-backpack equipment grenades are explicit cleanup
    exceptions. ``include_memory`` is reserved for Repair & Top Off's
    automatic Combine step; the standalone duplicate-removal command leaves
    Memory stacks protected.

    Only records that live directly in a top-level ``Items`` map, or within
    one weapon's own ``Attachments`` sub-map, are returned. Weapon attachment
    duplicates are only reported when they duplicate *within the same
    weapon* (see :func:`_find_same_weapon_attachment_duplicates`) -- the same
    attachment type equipped on two different weapons is not a duplicate,
    it is two separate weapons each legitimately carrying their own
    attachment.
    """
    extras = _all_duplicate_stack_candidates(document, include_memory=include_memory)
    if not extras:
        return extras
    purgeable, _blocked = _item_map_reach(document.raw)
    return [item for item in extras if item.entries["ItemCount"].offset in purgeable]


def _is_stack_record(offset: int, purgeable: set[int], blocked: list[tuple[int, int]]) -> bool:
    """Whether an ``ItemCount`` belongs to a real carried stack.

    True for entries of a top-level ``Items`` map or a weapon's own
    ``Attachments`` map, and (conservatively) for anything in the unread tail
    of such a map. Records nested inside another item's data -- the paints
    loaded in a paint item, the slots of a cartridge holder -- are contents of
    that item, not separate stacks, so they never count as duplicates.
    """
    return offset in purgeable or any(start <= offset < end for start, end in blocked)


def _all_duplicate_stack_candidates(
    document: SaveDocument, *, include_memory: bool = False,
) -> list[InventoryItem]:
    """Every redundant carried stack this scan can see, purgeable or not.

    This mirrors :func:`find_duplicate_stacks` but skips the final
    purgeability filter, so callers can tell the difference between "no
    duplicates exist" and "duplicates exist but this save's structure means
    they can't be safely auto-removed yet" (see
    :func:`count_unpurgeable_duplicate_stacks`).
    """
    purgeable, blocked = _item_map_reach(document.raw)
    stacks = [
        item for item in document.items
        if "ItemCount" in item.entries
        and _is_stack_record(item.entries["ItemCount"].offset, purgeable, blocked)
    ]
    groups: dict[tuple[str, object], list[InventoryItem]] = {}
    for item in stacks:
        if (
            not is_carried(item.location)
            or "ItemCount" not in item.entries
            or _is_weapon_attachment(item.asset, item.category)
        ):
            continue
        variation = item.entries.get("SelectedVariation")
        key = (item.asset, variation.value if variation is not None else None)
        groups.setdefault(key, []).append(item)
    extras: list[InventoryItem] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: int(item.entries["ItemCount"].value), reverse=True)
        extras.extend(ordered[1:])
    # Weapon attachments are never combined or deleted, including duplicates
    # nested inside the same weapon's Attachments map.
    protected = (
        _DUPLICATE_PROTECTED_CATEGORIES - {"memory"}
        if include_memory else _DUPLICATE_PROTECTED_CATEGORIES
    )
    explicit = [
        item for item in stacks
        if (
            item.asset.casefold() in _ALWAYS_REMOVE_DUPLICATES
            and item.location.casefold() == "player backpack"
            and item.category.casefold() == "equipment"
            and item.actor.casefold() == "bp_firstpersoncharacter_new_c1"
        )
    ]
    result: list[InventoryItem] = []
    seen_offsets: set[int] = set()
    for item in (*extras, *explicit):
        if _is_weapon_attachment(item.asset, item.category):
            continue
        if item.asset.casefold() not in _ALWAYS_REMOVE_DUPLICATES and not _is_grenade(item.asset):
            # Saves only use Item/Material/Equipment categories, so the
            # protected groups are also recognised by their asset prefix.
            if (
                item.category.casefold() in protected
                or item.asset.strip().casefold().startswith(tuple(protected))
            ):
                continue
        offset = item.entries["ItemCount"].offset
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        result.append(item)
    return result


def count_unpurgeable_duplicate_stacks(
    document: SaveDocument, *, include_memory: bool = False,
) -> int:
    """How many redundant carried stacks were detected but can't be removed.

    A handful of saves contain an ``Items``/``Attachments`` map with one
    entry this parser cannot fully decode; every entry *after* that point in
    the same map is skipped by the structural purge (see
    :func:`_purgeable_item_count_offsets`) even though the higher-level scan
    that builds :attr:`SaveDocument.items` can still see and report them as
    plain data. This lets the UI tell the user "duplicates exist but can't
    all be auto-removed yet" instead of the misleading "none found".
    """
    extras = _all_duplicate_stack_candidates(document, include_memory=include_memory)
    if not extras:
        return 0
    purgeable, _blocked = _item_map_reach(document.raw)
    return sum(1 for item in extras if item.entries["ItemCount"].offset not in purgeable)


def _find_same_weapon_attachment_duplicates(document: SaveDocument) -> list[InventoryItem]:
    """Return attachment records duplicated *within one weapon's* attachments.

    Each equipped weapon serializes its own ``Attachments`` map, holding the
    handful of attachment slots installed on that specific weapon. The same
    attachment type legitimately shows up once per weapon that carries it,
    so duplicates are only ever flagged when the same asset appears twice
    inside the very same weapon's map -- a data glitch, not two different
    weapons each wearing their own attachment.
    """
    try:
        maps = voyage_gvas.find_item_maps(document.raw, name="Attachments")
    except (voyage_gvas.GvasError, struct.error, ValueError):
        return []
    if not maps:
        return []
    spans = sorted((item_map.tag.start, item_map.tag.end) for item_map in maps)
    starts = [start for start, _end in spans]
    buckets: dict[int, list[InventoryItem]] = {}
    for item in document.items:
        if "ItemCount" not in item.entries:
            continue
        index = bisect.bisect_right(starts, item.anchor_offset) - 1
        if index < 0:
            continue
        start, end = spans[index]
        if start <= item.anchor_offset < end:
            buckets.setdefault(start, []).append(item)
    extras: list[InventoryItem] = []
    for group in buckets.values():
        by_asset: dict[str, list[InventoryItem]] = {}
        for item in group:
            by_asset.setdefault(item.asset, []).append(item)
        for same_asset in by_asset.values():
            if len(same_asset) < 2:
                continue
            ordered = sorted(
                same_asset, key=lambda item: int(item.entries["ItemCount"].value), reverse=True)
            extras.extend(ordered[1:])
    return extras


def _purgeable_item_count_offsets(raw: bytes) -> set[int]:
    """Offsets of every ``ItemCount`` field the structural purge can reach.

    ``purge_map_entries`` (the engine behind :func:`purge_items`) deletes
    entries of maps literally named ``Items`` or a weapon's own
    ``Attachments`` sub-map; other nesting is not reachable that way.

    A map is only included when this parser accounted for *every* entry it
    declares (``len(item_map.entries) == num_total`` read from the map's own
    header). Some saves contain a map with one entry this parser cannot
    fully decode; every entry after that point is still physically present
    in the file but invisible to a best-effort scan. Reporting those as
    "purgeable" here, only to have the structural removal refuse to touch
    the map (see ``remove_map_entries``), would make detection and removal
    disagree -- and actually removing from such a map risks orphaning real
    entries the map's rewritten header no longer accounts for, which is a
    genuine, previously-observed source of save corruption.
    """
    return _item_map_reach(raw)[0]


def _item_map_reach(raw: bytes) -> tuple[set[int], list[tuple[int, int]]]:
    """``(purgeable ItemCount offsets, unread map tails)`` for the item maps.

    The second value lists ``(start, end)`` byte ranges of maps this parser
    could only read partially: entries there are physically present but can't
    be enumerated, so they are reported rather than silently ignored.
    """
    offsets: set[int] = set()
    blocked: list[tuple[int, int]] = []
    for map_name in ("Items", "Attachments"):
        for item_map in voyage_gvas.find_item_maps(raw, name=map_name):
            num_total = struct.unpack_from("<ii", raw, item_map.tag.payload)[1]
            if len(item_map.entries) < num_total:
                tail = item_map.entries[-1].end if item_map.entries else item_map.tag.payload + 8
                blocked.append((tail, item_map.tag.end))
                continue
            for entry in item_map.entries:
                offset = voyage_gvas.entry_field_offsets(raw, entry).get("Data.ItemCount")
                if offset is not None:
                    offsets.add(offset)
    return offsets, blocked



def discover_saves(root: Path = DEFAULT_SAVE_ROOT) -> list[Path]:
    if not root.exists():
        return []
    saves = [
        path for path in root.rglob("*.sav")
        if ".bak" not in path.name.lower() and path.is_file()
    ]
    return sorted(saves, key=lambda path: path.stat().st_mtime, reverse=True)


def load_save(path: Path, progress: Callable[[str], None] | None = None) -> SaveDocument:
    """Read a save. ``progress`` receives a short stage label as work advances.

    It is called from whichever thread runs the load, so the callback must not
    touch UI objects.
    """
    def stage(text: str) -> None:
        if progress is not None:
            progress(text)

    stage("Decompressing the save")
    compressed = path.read_bytes()
    raw, chunk_size = decompress_container(compressed)
    validate_raw_save(raw)
    stage("Scanning values")
    entries = scan_scalars(raw)
    anchors = scan_text_anchors(raw)
    attach_context(entries, anchors)
    items = group_inventory(entries, anchors)
    mirrors: list[tuple[int, int, int]] = []
    item_maps: list[voyage_gvas.ItemMap] = []
    structure_problem = ""
    try:
        # The first walk of a large save takes about half a minute; later
        # loads of identical content reuse it.
        stage("Reading the item structure")
        spans = component_ranges(raw)
        refine_locations(items, spans)
        mirrors = find_mirrors(raw)
        item_maps = voyage_gvas.find_item_maps(raw)
        if not spans:
            structure_problem = "were not found"
    except (voyage_gvas.GvasError, struct.error, ValueError) as exc:
        structure_problem = f"could not be read ({exc})"
    stage("Reading humans")
    try:
        human_data = voyage_humans.read_humans(raw)
    except (voyage_gvas.GvasError, struct.error, ValueError) as exc:
        human_data = voyage_humans.HumanData(error=f"Human data could not be read: {exc}")
    stage("Finishing")
    warnings: list[str] = []
    if structure_problem and any(item.location == LOC_BACKPACK for item in items):
        warnings.append(
            f"the player's inventory components {structure_problem}; Holster, Equipped, "
            "Catalog and Module records may be listed as Backpack and structural "
            "tools may skip them"
        )
    resources = scan_resource_amounts(raw, anchors, items, item_maps)
    attach_resource_health(resources, entries, anchors, items)
    entries.extend(resource.entry for resource in resources)
    entries.sort(key=lambda entry: entry.offset)
    for index, entry in enumerate(entries):
        entry.index = index
    return SaveDocument(
        path=path,
        raw=raw,
        entries=entries,
        anchors=anchors,
        items=items,
        chunk_size=chunk_size,
        compressed_size=len(compressed),
        mirrors=mirrors,
        resources=resources,
        warnings=warnings,
        human_data=human_data,
    )


def parse_value(entry: Entry, value: object) -> int | float | bool:
    if entry.type == "BoolProperty":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{value!r} is not a boolean")
    if entry.type == "IntProperty":
        parsed = int(str(value).strip(), 0)
        if not -(2**31) <= parsed < 2**31:
            raise ValueError("Integer must fit in signed 32 bits")
        return parsed
    parsed = float(str(value).strip())
    if entry.name.startswith("Resources.") and (not math.isfinite(parsed) or parsed < 0):
        raise ValueError("Stored resource amount must be a finite, non-negative number")
    return parsed


def _encoded_value(entry: Entry, value: object) -> bytes:
    parsed = parse_value(entry, value)
    if entry.type == "IntProperty":
        return struct.pack("<i", parsed)
    if entry.type == "FloatProperty":
        return struct.pack("<f", parsed)
    if entry.type == "DoubleProperty":
        return struct.pack("<d", parsed)
    return bytes([1 if parsed else 0])


def backup_save(path: Path, max_backups: int = 20) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.{timestamp}.bak")
    shutil.copy2(path, backup)
    prune_backups(path, max_backups)
    return backup


def list_backups(path: Path) -> list[Path]:
    return sorted(
        path.parent.glob(f"{path.name}.*.bak"),
        key=lambda backup: backup.stat().st_mtime,
        reverse=True,
    )


def prune_backups(path: Path, max_backups: int) -> int:
    if max_backups < 1:
        raise ValueError("At least one backup must be retained")
    removed = 0
    for backup in list_backups(path)[max_backups:]:
        backup.unlink()
        removed += 1
    return removed


def save_is_stale(document: SaveDocument) -> bool:
    """True when the file changed on disk after ``document`` was loaded.

    The game rewrites its save while the editor holds an older copy in memory.
    Editing from that stale copy computes wrong edits and then fails the
    write's own consistency check, so callers reload first.
    """
    try:
        current, _chunk = decompress_container(document.path.read_bytes())
    except (OSError, SaveFormatError):
        return False
    return current != document.raw


def apply_edits(
    document: SaveDocument,
    edits: Iterable[tuple[Entry, object]],
    *,
    make_backup: bool = True,
    max_backups: int = 20,
) -> tuple[int, Path | None]:
    changed, _humans, backup = save_changes(
        document, edits, make_backup=make_backup, max_backups=max_backups)
    return changed, backup


def save_changes(
    document: SaveDocument,
    edits: Iterable[tuple[Entry, object]],
    human_changes: Mapping[voyage_humans.HumanKey, str | None] | None = None,
    *,
    human_attributes: Mapping[voyage_humans.AttributeKey, object] | None = None,
    make_backup: bool = True,
    max_backups: int = 20,
) -> tuple[int, int, Path | None]:
    """Write value edits, human stats and professions as one verified save.

    Returns ``(scalar/stat fields changed, professions changed, backup)``.
    The profession count is per human. No write or backup occurs on a no-op.
    """
    current = document.path.read_bytes()
    current_raw, current_chunk_size = decompress_container(current)
    if current_raw != document.raw:
        raise RuntimeError("Save changed on disk since it was loaded; reload before editing")
    raw = bytearray(document.raw)
    changed = 0
    edits_list = list(edits)
    for entry, value in edits_list:
        encoded = _encoded_value(entry, value)
        offsets = [entry.offset]
        # The backpack is serialized twice; keep both copies in step.
        twin = mirror_offset(document.mirrors, entry.offset)
        if twin is not None:
            offsets.append(twin)
        touched = False
        for offset in offsets:
            end = offset + len(encoded)
            if end > len(raw):
                raise SaveFormatError(f"Edit for {entry.name} is outside the save")
            if raw[offset:end] != encoded:
                raw[offset:end] = encoded
                touched = True
        if touched:
            changed += 1
    new_raw = bytes(raw)
    if human_attributes:
        new_raw, stats_changed = voyage_humans.apply_attributes(new_raw, human_attributes)
        changed += stats_changed
    humans_changed = 0
    if human_changes:
        # Same-width value edits keep every offset, so the structural trait
        # rewrite runs last against the already-edited bytes.
        new_raw, humans_changed = voyage_humans.apply_professions(new_raw, human_changes)
    if not changed and not humans_changed:
        return 0, 0, None
    backup = _write_raw(document, new_raw, current_chunk_size, make_backup, max_backups)
    # Same-width value writes keep every offset, so the (slow) structural walk
    # of the old save stays valid for the reload -- except BoolProperty, whose
    # value is stored in the tag's flags byte, and human trait rewrites, which
    # change sizes.
    if not humans_changed and all(entry.type != "BoolProperty" for entry, _value in edits_list):
        voyage_gvas.carry_item_map_cache(document.raw, new_raw)
    return changed, humans_changed, backup


def set_human_profession(
    document: SaveDocument,
    keys: Iterable[voyage_humans.HumanKey],
    profession: str | None,
    *,
    make_backup: bool = True,
    max_backups: int = 20,
) -> tuple[int, Path | None]:
    """Save a verified roster/pod profession change, refusing stale documents."""
    if profession is not None and profession not in voyage_humans.PROFESSIONS:
        raise ValueError("Choose a profession from the supported v6 catalogue")
    _edits, changed, backup = save_changes(
        document, (), dict.fromkeys(keys, profession),
        make_backup=make_backup, max_backups=max_backups)
    return changed, backup


def _write_raw(document: SaveDocument, raw: bytes, chunk_size: int,
               make_backup: bool, max_backups: int) -> Path | None:
    validate_raw_save(raw)
    replacement = compress_container(raw, chunk_size)
    roundtrip, _ = decompress_container(replacement)
    if roundtrip != raw:
        raise RuntimeError("Internal compression verification failed")
    backup = backup_save(document.path, max_backups) if make_backup else None
    temporary = document.path.with_name(document.path.name + ".editor.tmp")
    try:
        temporary.write_bytes(replacement)
        os.replace(temporary, document.path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup


def write_raw_changes(
    document: SaveDocument,
    raw: bytes,
    *,
    max_backups: int = 20,
) -> Path | None:
    """Write a verified structural edit after checking the document is current."""
    validate_raw_save(raw)
    if max_backups < 1:
        raise ValueError("At least one backup must be retained")
    current = document.path.read_bytes()
    current_raw, chunk_size = decompress_container(current)
    if current_raw != document.raw:
        raise RuntimeError("Save changed on disk since it was loaded; reload before editing")
    if raw == document.raw:
        return None
    return _write_raw(document, raw, chunk_size, True, max_backups)


def _attachment_protector(raw: bytes):
    """Build ``protects(item_map, entry, values)`` for records purging must keep.

    Attachments are never deleted, and neither is any record that holds
    installed attachments (removing a weapon would take them with it).
    """
    attachment_maps = [
        item_map for item_map in voyage_gvas.find_item_maps(raw, name="Attachments")
        if item_map.entries
    ]

    def protects(item_map: "voyage_gvas.ItemMap",
                 entry: "voyage_gvas.MapEntry", values: dict) -> bool:
        if item_map.tag.name == "Attachments":
            return True
        if "attachment" in str(values.get("AssetName", "")).casefold():
            return True
        return any(
            entry.start <= nested.tag.start < entry.end for nested in attachment_maps
        )

    return protects


def purge_items(
    document: SaveDocument,
    items: Iterable[InventoryItem] | None = None,
    *,
    zero_count_only: bool = False,
    player_only: bool = True,
    make_backup: bool = True,
    max_backups: int = 20,
) -> tuple[int, Path | None, list[tuple[str, int]]]:
    """Structurally remove inventory entries instead of zeroing their count.

    Zeroing an entry leaves a grayed-out, unusable slot in game.  This deletes
    the map entry outright and shrinks every enclosing container header so the
    save still loads.

    ``items`` selects specific records (matched on their ``ItemCount`` offset);
    pass ``None`` together with ``zero_count_only`` to sweep every empty slot.
    """
    current = document.path.read_bytes()
    current_raw, current_chunk_size = decompress_container(current)
    if current_raw != document.raw:
        raise RuntimeError("Save changed on disk since it was loaded; reload before editing")

    targets: set[int] | None = None
    if items is not None:
        targets = set()
        for item in items:
            entry = item.entries.get("ItemCount")
            if entry is None:
                continue
            targets.add(entry.offset)
            twin = mirror_offset(document.mirrors, entry.offset)
            if twin is not None:
                targets.add(twin)
        if not targets:
            return 0, None, []

    raw = document.raw
    # A mirrored copy must lose the same entries, or the two diverge.
    mirrored_spans = document.mirrors
    protects_attachments = _attachment_protector(raw)

    def in_scope(item_map: "voyage_gvas.ItemMap") -> bool:
        if _is_carried_map(item_map):
            return True
        return any(
            start <= item_map.tag.payload < end
            for start, end, _delta in mirrored_spans
        )

    def predicate(item_map: "voyage_gvas.ItemMap", entry: "voyage_gvas.MapEntry",
                  values: dict) -> bool:
        if protects_attachments(item_map, entry, values):
            return False
        if player_only and not in_scope(item_map):
            return False
        if zero_count_only and values.get("Data.ItemCount") != 0:
            return False
        if targets is None:
            return True
        offsets = voyage_gvas.entry_field_offsets(raw, entry)
        return offsets.get("Data.ItemCount") in targets

    new_raw, removed, report = voyage_gvas.purge_map_entries(
        raw, predicate, names=("Items", "Attachments"))
    if not removed:
        return 0, None, []
    validate_raw_save(new_raw)
    # Re-walking proves every Size header we touched is still consistent.
    voyage_gvas.find_item_maps(new_raw)
    backup = _write_raw(document, new_raw, current_chunk_size, make_backup, max_backups)
    # Report the records the player sees, not the duplicate mirror writes, and
    # aggregate world/environment maps by asset name instead of one row per
    # underlying actor (there can be hundreds of otherwise-identical pickups).
    visible_counts: dict[str, int] = {}
    for item_map, count in report:
        if not _is_carried_map(item_map) and any(
            start <= item_map.tag.payload < end for start, end, _delta in mirrored_spans
        ):
            # The module-side twin of a carried map: same records, counted once.
            continue
        if _is_carried_map(item_map):
            label = player_component_location(item_map)
        elif item_map.tag.name == "Attachments":
            label = "Weapon Attachments"
        else:
            asset_names = {
                voyage_gvas.entry_values(raw, entry).get("AssetName")
                for entry in item_map.entries
            }
            asset_names.discard(None)
            label = f"World: {next(iter(asset_names))}" if len(asset_names) == 1 else "World items"
        visible_counts[label] = visible_counts.get(label, 0) + count
    visible = sorted(visible_counts.items())
    return sum(count for _label, count in visible), backup, visible


SCAN_ASSET_TYPE = "Item"
_SCANNED_TAG = b"\x08\x00\x00\x00Scanned\x00"
_BLUEPRINT_NAME = re.compile(rb"(DA_[A-Za-z0-9_\-]*(?:Blueprint|BP)[A-Za-z0-9_\-]*)\x00")


def _pack_fstring(text: str) -> bytes:
    data = text.encode("ascii") + b"\x00"
    return struct.pack("<i", len(data)) + data


def _pack_type(name: str, *children: bytes) -> bytes:
    return _pack_fstring(name) + struct.pack("<i", len(children)) + b"".join(children)


def _pack_tag(name: str, type_bytes: bytes, payload: bytes) -> bytes:
    return _pack_fstring(name) + type_bytes + struct.pack("<iB", len(payload), 0) + payload


def build_scan_entry(asset_name: str, when: int, *, asset_type: str = SCAN_ASSET_TYPE,
                     scan_count: int = 1) -> bytes:
    """Serialize one ``Scanned`` map entry (PrimaryAssetId -> VoyageScanData)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", asset_name):
        raise ValueError(f"Unsupported asset name {asset_name!r}")
    name_type = _pack_type("NameProperty")
    type_struct = _pack_type(
        "StructProperty", _pack_type("PrimaryAssetType", _pack_type("/Script/CoreUObject")))
    type_payload = _pack_tag("Name", name_type, _pack_fstring(asset_type)) + _pack_fstring("None")
    key = (
        _pack_tag("PrimaryAssetType", type_struct, type_payload)
        + _pack_tag("PrimaryAssetName", name_type, _pack_fstring(asset_name))
        + _pack_fstring("None")
    )
    int64 = _pack_type("Int64Property")
    value = (
        _pack_tag("FirstScanTimeUtc", int64, struct.pack("<q", when))
        + _pack_tag("LastScanTimeUtc", int64, struct.pack("<q", when))
        + _pack_tag("ScanCount", _pack_type("IntProperty"), struct.pack("<i", scan_count))
        + _pack_fstring("None")
    )
    return key + value


def find_scanned_map(raw: bytes) -> int:
    """Return the tag offset of the single ``Scanned`` (VoyageScanData) map."""
    found = []
    for match in re.finditer(re.escape(_SCANNED_TAG), raw):
        try:
            tag, _ = voyage_gvas.read_tag(raw, match.start())
        except voyage_gvas.GvasError:
            continue
        if (tag is not None and tag.type_name == "MapProperty"
                and len(tag.type.children) == 2
                and tag.type.children[1].first is not None
                and tag.type.children[1].first.name == "VoyageScanData"):
            found.append(match.start())
    if len(found) != 1:
        raise SaveFormatError(f"Expected one Scanned map, found {len(found)}")
    return found[0]


def scanned_assets(raw: bytes) -> dict[str, dict]:
    """``{asset name: {"FirstScanTimeUtc": ..., ...}}`` for every scanned asset."""
    start = find_scanned_map(raw)
    tag = voyage_gvas.find_path(raw, start)[-1]
    _num_remove, entries = voyage_gvas.parse_map(raw, tag)
    out: dict[str, dict] = {}
    for entry in entries:
        key = voyage_gvas._flatten(raw, entry.key, "", {})
        name = key.get("PrimaryAssetName")
        if name:
            out[str(name)] = voyage_gvas.entry_values(raw, entry)
    return out


def known_blueprint_assets(raw: bytes) -> list[str]:
    """Blueprint item assets referenced anywhere in the save (memories excluded)."""
    names = set()
    for match in _BLUEPRINT_NAME.finditer(raw):
        name = match.group(1)
        prefix = match.start(1) - 4
        if prefix < 0 or struct.unpack_from("<i", raw, prefix)[0] != len(name) + 1:
            continue
        text = name.decode("ascii")
        folded = text.casefold()
        if folded.startswith(("da_memory", "da_quest", "da_location", "da_skill")):
            continue
        if "blueprint" in folded or folded.endswith("bp"):
            names.add(text)
    return sorted(names, key=str.casefold)


def blueprint_scan_status(document: SaveDocument) -> list[tuple[str, bool]]:
    scanned = scanned_assets(document.raw)
    return [(name, name in scanned) for name in known_blueprint_assets(document.raw)]


def mark_blueprints_scanned(
    document: SaveDocument,
    assets: Iterable[str] | None = None,
    *,
    when: int | None = None,
    make_backup: bool = True,
    max_backups: int = 20,
) -> tuple[list[str], Path | None]:
    """Add ``Scanned`` records for unscanned blueprints and write the save."""
    current = document.path.read_bytes()
    current_raw, current_chunk_size = decompress_container(current)
    if current_raw != document.raw:
        raise RuntimeError("Save changed on disk since it was loaded; reload before editing")
    raw = document.raw
    scanned = scanned_assets(raw)
    wanted = known_blueprint_assets(raw) if assets is None else list(assets)
    missing = sorted({name for name in wanted if name not in scanned}, key=str.casefold)
    if not missing:
        return [], None
    stamp = int(time.time()) if when is None else int(when)
    blobs = [build_scan_entry(name, stamp) for name in missing]
    new_raw = voyage_gvas.append_map_entries(raw, find_scanned_map(raw), blobs)
    validate_raw_save(new_raw)
    after = scanned_assets(new_raw)
    if any(name not in after for name in missing) or len(after) != len(scanned) + len(missing):
        raise RuntimeError("Scanned records did not verify after insertion")
    backup = _write_raw(document, new_raw, current_chunk_size, make_backup, max_backups)
    return missing, backup



def _is_carried_map(item_map: "voyage_gvas.ItemMap") -> bool:
    """True for the pawn components the player can actually open in game."""
    return (
        is_player_map(item_map)
        and is_carried(player_component_location(item_map))
    )


def count_empty_slots(document: SaveDocument, *, player_only: bool = True) -> int:
    """How many zero-count entries :func:`purge_items` would delete.

    Records purging protects (attachments, weapons holding attachments) are
    left out, so the figure shown before a purge matches what it removes.
    """
    protects = _attachment_protector(document.raw)
    total = 0
    for item_map in voyage_gvas.find_item_maps(document.raw):
        if player_only and not _is_carried_map(item_map):
            continue
        for entry in item_map.entries:
            values = voyage_gvas.entry_values(document.raw, entry)
            if values.get("Data.ItemCount") == 0 and not protects(item_map, entry, values):
                total += 1
    return total


def restore_backup(path: Path, backup: Path) -> Path:
    if backup not in list_backups(path):
        raise ValueError("Selected file is not a backup of this save")
    raw, _ = decompress_container(backup.read_bytes())
    validate_raw_save(raw)
    safety = backup_save(path)
    temporary = path.with_name(path.name + ".restore.tmp")
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return safety


def summary(document: SaveDocument) -> dict[str, object]:
    return {
        "path": str(document.path),
        "compressed_size": document.compressed_size,
        "decompressed_size": len(document.raw),
        "chunk_size": document.chunk_size,
        "editable_scalars": len(document.entries),
        "inventory_items": len(document.items),
        "scalar_types": dict(Counter(entry.type for entry in document.entries)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("save", type=Path, nargs="?")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("info")
    dump_parser = subparsers.add_parser("dump")
    dump_parser.add_argument("--out", type=Path)
    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("--name", required=True)
    set_parser.add_argument("--value", required=True)
    set_parser.add_argument("--context")
    set_parser.add_argument("--all", action="store_true")
    purge_parser = subparsers.add_parser(
        "purge", help="permanently remove empty player inventory slots")
    purge_parser.add_argument(
        "--dry-run", action="store_true", help="only report how many would be removed")
    purge_parser.add_argument(
        "--everywhere", action="store_true",
        help="also purge world containers, not just the player inventory")
    scan_parser = subparsers.add_parser(
        "blueprints", help="list blueprints and optionally mark them scanned")
    scan_parser.add_argument(
        "--mark-scanned", action="store_true",
        help="add Scanned records for every unscanned blueprint (backup first)")
    lock_parser = subparsers.add_parser(
        "locks", help="show or toggle infinite health / infinite energy")
    for key in PLAYER_LOCKS:
        lock_parser.add_argument(f"--{key}", choices=("on", "off"),
                                 help=f"turn infinite {key} on or off (backup first)")
    args = parser.parse_args(argv)
    path = args.save or (discover_saves()[0] if discover_saves() else None)
    if path is None:
        parser.error("No save found; pass a .sav path")
    document = load_save(path)
    if args.command in (None, "info"):
        print(json.dumps(summary(document), indent=2))
        return 0
    if args.command == "dump":
        payload = [asdict(entry) for entry in document.entries]
        text = json.dumps(payload, indent=2)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    if args.command == "purge":
        player_only = not args.everywhere
        empty = count_empty_slots(document, player_only=player_only)
        if args.dry_run:
            print(f"{empty} empty slot(s) would be removed")
            return 0
        removed, backup, report = purge_items(
            document, zero_count_only=True, player_only=player_only)
        print(f"Removed {removed} record(s). Backup: {backup}")
        for owner, count in report:
            print(f"  {owner}: {count}")
        return 0
    if args.command == "blueprints":
        for name, scanned in blueprint_scan_status(document):
            print(f"{'scanned  ' if scanned else 'UNSCANNED'} {name}")
        if args.mark_scanned:
            added, backup = mark_blueprints_scanned(document)
            print(f"Marked {len(added)} blueprint(s) scanned. Backup: {backup}")
        return 0
    if args.command == "locks":
        for key in PLAYER_LOCKS:
            choice = getattr(args, key)
            if choice:
                changed, backup = set_player_lock(document, key, choice == "on")
                print(f"Infinite {key} {choice}: {changed} value(s) written. Backup: {backup}")
                document = load_save(document.path)
        for key, locked in player_lock_states(document).items():
            print(f"Infinite {key}: {'ON' if locked else 'off'}")
        return 0
    matches = [
        entry for entry in document.entries
        if entry.name.casefold() == args.name.casefold()
        and (not args.context or args.context.casefold() in entry.context.casefold())
    ]
    if not matches:
        raise SystemExit("No matching scalar found")
    if len(matches) > 1 and not args.all:
        raise SystemExit(f"{len(matches)} matches found; add --context or --all")
    changed, backup = apply_edits(
        document, [(entry, args.value) for entry in matches]
    )
    print(f"Changed {changed} value(s). Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
