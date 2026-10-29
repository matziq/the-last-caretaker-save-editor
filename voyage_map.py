"""Saved positions of persistent actors, not a reconstruction of terrain."""
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import voyage_gvas as gvas
import voyage_humans as humans


NAMED_LOCATIONS_PATH = Path(__file__).with_name("named_locations.json")
LAYERS = (
    ("player", "Player", "#205da8", True),
    ("humans", "Humans in pods", "#a94e09", True),
    ("locations", "Named locations", "#a3324a", True),
    ("pods", "Other human pods", "#197265", False),
    ("rockets", "Functional rockets", "#75449b", False),
    ("checkpoints", "Checkpoints", "#52701e", False),
    ("containers", "Containers / crates", "#566c81", False),
    ("other", "Other saved actors", "#727b85", False),
)
LAYER_NAMES = {key: label for key, label, _color, _enabled in LAYERS}
MOVABLE_ACTOR_PREFIXES = ("BP_Item_HumanSeed_", "BP_Module_Human_Pod_")


@dataclass(frozen=True)
class MapPoint:
    key: str
    world: str
    actor: str
    label: str
    layer: str
    position: humans.Position3D
    social_id: str = ""
    profession: str = ""
    details: tuple[str, ...] = ()
    actor_offset: int | None = None
    move_block_reason: str = "Only loose human seed items and human pods can be moved."

    @property
    def searchable(self) -> str:
        return " ".join((self.label, self.actor, self.world, self.social_id, self.profession,
                         *self.details))


@dataclass(frozen=True)
class NamedLocation:
    actor: str
    world: str
    name: str
    ident: str
    position: humans.Position3D


@dataclass(frozen=True)
class MapData:
    points: tuple[MapPoint, ...] = ()
    worlds: tuple[str, ...] = ()
    actor_count: int = 0
    skipped_positions: int = 0
    warnings: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()

    def player(self, world: str) -> MapPoint | None:
        players = [point for point in self.points if point.world == world and point.layer == "player"]
        return players[0] if len(players) == 1 else None

    def distance(self, point: MapPoint) -> float | None:
        player = self.player(point.world)
        return math.dist(point.position, player.position) if player is not None else None


def _actor_label(name: str) -> str:
    short = re.split(r"_C(?:_|-|\d|$)", name, maxsplit=1)[0]
    return short.removeprefix("BP_").replace("_", " ")


def load_named_locations(path: Path = NAMED_LOCATIONS_PATH) -> tuple[NamedLocation, ...]:
    """Positions of named places extracted from the game by extract_named_locations.py."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"Named locations unavailable: {path.name} could not be read ({exc})") from exc
    return _load_named_locations(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_named_locations(path: Path, _mtime: int, _size: int) -> tuple[NamedLocation, ...]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["locations"]
        locations = tuple(
            NamedLocation(entry["actor"], entry["world"], entry["name"], entry.get("ident", ""),
                          (float(entry["x"]), float(entry["y"]), float(entry["z"])))
            for entry in entries)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Named locations unavailable: {path.name} is invalid ({exc})") from exc
    if not all(isinstance(value, str) and value for location in locations
               for value in (location.actor, location.world, location.name)) or not all(
            math.isfinite(value) for location in locations for value in location.position):
        raise ValueError(f"Named locations unavailable: {path.name} has an invalid entry")
    return locations


def _saved_locators(raw: bytes, props: list[gvas.PropertyTag]) -> set[str] | None:
    tag = humans._field(props, "Locators", required=False)
    if tag is None:
        return set()
    try:
        records, _ = humans._struct_records(raw, tag)
    except (gvas.GvasError, struct.error, ValueError):
        return None
    return {match.group(0) for record in records if isinstance(record.key, str)
            for match in [re.search(r"VoyageLocatorActor_UAID_\w+$", record.key)] if match}


def _packed_transform(props: list[gvas.PropertyTag], name: str) -> gvas.PropertyTag:
    transform = humans._field(props, name, required=False)
    if (transform is None or transform.type_name != "StructProperty"
            or transform.type.first is None
            or transform.type.first.name != "VoyagePackedTransform"
            or transform.size != 36):
        raise ValueError(f"{name} is not a supported saved transform")
    return transform


def _relocation_transforms(
    raw: bytes, props: list[gvas.PropertyTag],
) -> tuple[gvas.PropertyTag, gvas.PropertyTag]:
    parent = humans._field(props, "AttachParent", required=False)
    if parent is None or parent.type_name != "OptionalProperty" or parent.size != 4:
        raise ValueError("Attachment state is unavailable; this object cannot be moved safely.")
    if raw[parent.payload:parent.end] != b"\0" * 4:
        raise ValueError("This object is attached to another actor and cannot be moved safely.")

    actor_transform = _packed_transform(props, "Transform")
    actor_position = struct.unpack_from("<3d", raw, actor_transform.payload)
    component_map = humans._field(props, "ComponentTransforms", required=False)
    if component_map is None or component_map.type_name != "MapProperty":
        raise ValueError("Component transforms are unavailable; this object cannot be moved safely.")
    components = humans._map_entries(raw, component_map)
    if len(components) != 1:
        raise ValueError("This object has an unsupported component layout and cannot be moved safely.")
    component_props, component_end = gvas.read_property_list(raw, components[0].value_start)
    if component_end != components[0].end:
        raise ValueError("Component transform data is incomplete; this object cannot be moved safely.")
    component_transform = _packed_transform(component_props, "Transform")
    if struct.unpack_from("<3d", raw, component_transform.payload) != actor_position:
        raise ValueError("Actor and component positions disagree; this object cannot be moved safely.")

    for field in ("ComponentTransformsNonPhysical", "BoneTransforms"):
        tag = humans._field(props, field, required=False)
        if tag is None or tag.type_name != "MapProperty" or humans._map_entries(raw, tag):
            raise ValueError(f"{field} contains unsupported saved transforms.")
    return actor_transform, component_transform


def _move_block_reason(raw: bytes, props: list[gvas.PropertyTag], name: str) -> str:
    if not name.startswith(MOVABLE_ACTOR_PREFIXES):
        return "Only loose human seed items and human pods can be moved."
    try:
        _relocation_transforms(raw, props)
    except (ValueError, gvas.GvasError, struct.error) as exc:
        return str(exc)
    return ""


def relocate_actor(
    raw: bytes, point: MapPoint, destination: humans.Position3D,
) -> bytes:
    """Move one supported loose human pod/seed, keeping its save cell unchanged.

    Destination coordinates are meters; the packed save transforms are centimeters.
    """
    if point.actor_offset is None:
        raise ValueError("This map marker is not a movable saved actor.")
    if point.move_block_reason:
        raise ValueError(point.move_block_reason)
    if (len(destination) != 3
            or not all(isinstance(value, (int, float)) and math.isfinite(value)
                       for value in destination)):
        raise ValueError("Destination must contain three finite coordinates.")
    if not point.actor.startswith(MOVABLE_ACTOR_PREFIXES):
        raise ValueError("Only loose human seed items and human pods can be moved.")

    target_cm = tuple(value * 100 for value in destination)
    target_cell = f"{math.floor(target_cm[0] / 10000)}:{math.floor(target_cm[1] / 10000)}"
    root, _ = gvas.read_property_list(raw, gvas.find_root(raw))
    progress = humans._field(root, "GameProgress")
    worlds, _ = humans._struct_records(raw, progress)
    world_matches = [world for world in worlds if world.key == point.world]
    if len(world_matches) != 1:
        raise ValueError("The selected object's world is no longer available.")
    persistent = humans._field(world_matches[0].props, "PersistentActors", required=False)
    if persistent is None:
        raise ValueError("The selected object's saved actor list is unavailable.")
    cells, _ = humans._struct_records(raw, persistent)
    actor_matches = []
    source_cell = None
    for cell in cells:
        actors_tag = humans._field(cell.props, "actors", required=False)
        if actors_tag is None:
            continue
        actors, _ = humans._struct_records(raw, actors_tag)
        for actor in actors:
            if actor.value_start == point.actor_offset:
                actor_matches.append(actor)
                source_cell = cell
    if len(actor_matches) != 1 or source_cell is None:
        raise ValueError("The selected object could not be found at its saved position.")
    if source_cell.key != target_cell:
        raise ValueError(
            "The destination crosses a saved world-cell boundary. Choose a closer position "
            "so the actor's cell and related save data remain consistent."
        )
    actor = actor_matches[0]
    name_tag = humans._field(actor.props, "ActorName", required=False)
    if name_tag is None or humans._string(raw, name_tag) != point.actor:
        raise ValueError("The selected actor no longer matches this map marker.")
    actor_transform, component_transform = _relocation_transforms(raw, actor.props)
    expected_position = tuple(value * 100 for value in point.position)
    current_position = struct.unpack_from("<3d", raw, actor_transform.payload)
    if current_position != expected_position:
        raise ValueError("The selected object's position changed; refresh the map before moving it.")

    updated = bytearray(raw)
    for transform in (actor_transform, component_transform):
        struct.pack_into("<3d", updated, transform.payload, *target_cm)
    return bytes(updated)


def read_map(
    raw: bytes, human_data: humans.HumanData | None = None,
    *, progress: Callable[[str], None] | None = None,
    named_locations: Path | None = None,
) -> MapData:
    if len(raw) < 4 or struct.unpack_from("<I", raw)[0] != len(raw) - 4:
        raise gvas.GvasError("Invalid save length envelope")
    root, _ = gvas.read_property_list(raw, gvas.find_root(raw))
    game_progress = humans._field(root, "GameProgress")
    if game_progress.type_name == "StructProperty":
        props, end = gvas.read_property_list(raw, game_progress.payload)
        if end != game_progress.end:
            raise gvas.GvasError("GameProgress does not consume its payload")
        worlds = [gvas.MapEntry(0, game_progress.payload, end, "World", game_progress.payload, props)]
    else:
        worlds, _ = humans._struct_records(raw, game_progress)
    roster = {human.key: human for human in (human_data or humans.HumanData()).humans}
    points: list[MapPoint] = []
    world_names: list[str] = []
    warnings: list[str] = []
    notices: list[str] = []
    locations: tuple[NamedLocation, ...] = ()
    if named_locations is not None:
        try:
            locations = load_named_locations(named_locations)
        except ValueError as exc:
            notices.append(str(exc))
    actor_count = skipped = 0
    for world_entry in worlds:
        world = world_entry.key
        if not isinstance(world, str) or world in world_names:
            raise gvas.GvasError("Invalid or duplicate map world")
        world_names.append(world)
        world_locations = [item for item in locations if item.world.casefold() == world.casefold()]
        saved = _saved_locators(raw, world_entry.props) if world_locations else set()
        for location in world_locations:
            listed = ("unknown (save list unreadable)" if saved is None
                      else "yes" if location.actor in saved else "no")
            points.append(MapPoint(
                f"{world}:location:{location.actor}", world, location.actor, location.name,
                "locations", location.position,
                details=tuple(filter(None, (
                    "Position source: game world data (not stored in the save)",
                    f"In this save's map-marker list: {listed}",
                    f"Game identifier: {location.ident}" if location.ident else ""))),
            ))
        persistent = humans._field(world_entry.props, "PersistentActors", required=False)
        if persistent is None:
            continue
        cells, _ = humans._struct_records(raw, persistent)
        for index, cell in enumerate(cells):
            if progress is not None and index % 100 == 0:
                progress(f"Reading map positions: {world}, cell {index + 1:,}/{len(cells):,}")
            actors_tag = humans._field(cell.props, "actors", required=False)
            if actors_tag is None:
                continue
            actors, _ = humans._struct_records(raw, actors_tag)
            for actor in actors:
                actor_count += 1
                name_tag = humans._field(actor.props, "ActorName", required=False)
                position = humans._actor_position(raw, actor.props)
                if name_tag is None or position is None:
                    skipped += 1
                    continue
                name = humans._string(raw, name_tag)
                layer, label, social_id, profession = "other", _actor_label(name), "", ""
                if name.startswith("BP_FirstPersonCharacter"):
                    layer, label = "player", "Saved player position"
                elif name.startswith("BP_Module_Human_Pod_"):
                    try:
                        blob = humans._field(actor.props, "ByteData")
                        props = humans._blob_properties(raw, blob)
                        occupied = humans._has_human(raw, humans._field(props, "bHasHumanCurrent"))
                        if occupied:
                            social_id = humans._string(raw, humans._field(props, "HumanId"))
                            layer, label = "humans", f"Human in pod: {social_id[:8] or 'unknown ID'}"
                            human = roster.get((world, social_id))
                            if human is not None:
                                profession = "; ".join(map(humans.profession_label, human.professions))
                        else:
                            layer, label = "pods", "Human pod (no grown human)"
                    except (gvas.GvasError, struct.error) as exc:
                        label = "Human pod (state unavailable)"
                        warnings.append(f"{name}: {exc}")
                elif name.startswith("BP_Rocket_Functional_C"):
                    layer, label = "rockets", "Functional rocket"
                elif name.startswith("BP_Module_Checkpoint_"):
                    layer, label = "checkpoints", "Checkpoint"
                elif name.startswith(("BP_Inventory_Container_", "BP_Inventory_PlayerBox_")):
                    layer = "containers"
                x, y, z = position
                points.append(MapPoint(
                    f"{world}:{actor.start}", world, name, label, layer,
                    (x / 100, y / 100, z / 100), social_id, profession,
                    actor_offset=actor.value_start,
                    move_block_reason=_move_block_reason(raw, actor.props, name),
                ))
    return MapData(tuple(points), tuple(world_names), actor_count, skipped, tuple(warnings),
                   tuple(notices))


@dataclass
class MapCamera:
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0

    def screen(self, x: float, y: float, width: float, height: float) -> tuple[float, float]:
        # North is -Y in this world (North Bastion/Lantern are at negative Y,
        # South Echo at positive Y), so +Y runs down the screen: north-up and,
        # like Unreal's top view, not mirrored.
        return width / 2 + (x - self.x) * self.scale, height / 2 + (y - self.y) * self.scale

    def world(self, x: float, y: float, width: float, height: float) -> tuple[float, float]:
        return self.x + (x - width / 2) / self.scale, self.y + (y - height / 2) / self.scale

    def fit(self, positions: list[humans.Position3D], width: float, height: float) -> None:
        if not positions:
            self.x, self.y, self.scale = 0.0, 0.0, 1.0
            return
        xs, ys = [p[0] for p in positions], [p[1] for p in positions]
        self.x, self.y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        self.scale = min(max(width - 100, 1) / max(max(xs) - min(xs), 100),
                         max(height - 100, 1) / max(max(ys) - min(ys), 100))
        self.scale = min(max(self.scale, 0.00001), 1000)

    def zoom(self, factor: float, px: float, py: float, width: float, height: float) -> None:
        before = self.world(px, py, width, height)
        self.scale = min(max(self.scale * factor, 0.00001), 1000)
        after = self.world(px, py, width, height)
        self.x += before[0] - after[0]
        self.y += before[1] - after[1]

    def pan(self, dx: float, dy: float) -> None:
        self.x -= dx / self.scale
        self.y -= dy / self.scale
