"""Human rosters, raw stats, v6 professions, and verified human edits."""
from __future__ import annotations

import math
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping, overload

import voyage_gvas as gvas


PROFESSION_ROOT = "/Game/Data/Assets/HumanGrow/Traits/Professions/"
_PROFESSION_GROUPS = {
    "Supplier": ("Distributor", "Basic_Supplier", "Logistics_High_Command", "Resource_Director"),
    "Healer": ("Doctor", "Neuro_Specialist", "Health_Assistant", "Neural_Architect"),
    "Protector": ("GuardianofHumanity", "Station_Protector", "Guard", "Door_Jammer"),
    "Engineer": ("Energy_Engineer", "Engineer_Maintenance", "Quantum_Engineer", "Systems_Engineer"),
    "Scientist": ("Quantum_Physicist", "Field_Research_Scientist", "Theoretical_Scientist", "Lab_Technician"),
    "Explorer": ("Station_Roamer", "Frontier_Explorer", "Star_Analyzer", "Mission_Seeker"),
    "Teacher": ("Professor", "Existential_Chancellor", "Manual_Holder", "Teacher"),
    "Leader": ("Room_Supervisor", "Station_Quartermaster", "Colonel_of_Humanity", "Settlement_Governor"),
    "StarChild": ("StarChild",),
    "Cultivators": ("Nutrient_Handler", "Biosphere_Director", "Growth_Specialist", "Sustenance_Architect"),
    "Artist": ("Cultural_Archivist", "Sculptor", "Existential_Expressionist", "Visual_Technician"),
}
_PROFESSION_LABELS = {
    "GuardianofHumanity": "Guardian of Humanity",
    "Engineer_Maintenance": "Maintenance Engineer",
    "StarChild": "Star Child",
}
PROFESSIONS = {
    f"{PROFESSION_ROOT}{group}/DA_Profession_{name}.DA_Profession_{name}":
    _PROFESSION_LABELS.get(name, name.replace("_", " "))
    for group, names in _PROFESSION_GROUPS.items() for name in names
}
PROFESSION_ASSETS = {label: asset for asset, label in PROFESSIONS.items()}


@dataclass(frozen=True)
class Committee:
    key: str
    name: str
    tier: int
    professions: tuple[str, ...]
    minimum_age: int = 18


# Verified against DA_HumanGrow_Metadata and the profession assets in v6.0.
COMMITTEES = (
    Committee("DECK_OPS", "Deck Operations", 1,
              ("Maintenance Engineer", "Basic Supplier", "Nutrient Handler", "Door Jammer")),
    Committee("HAB_CARE", "Habitat Care", 1,
              ("Room Supervisor", "Health Assistant", "Teacher", "Lab Technician")),
    Committee("TRANS_AND_DIST", "Transit & Distribution", 1,
              ("Systems Engineer", "Distributor", "Growth Specialist", "Guard")),
    Committee("POW_AND_SEC", "Power & Security", 1,
              ("Energy Engineer", "Resource Director", "Station Quartermaster", "Station Protector")),
    Committee("COG_RES", "Cognitive Resilience", 2,
              ("Theoretical Scientist", "Neuro Specialist", "Professor", "Star Analyzer")),
    Committee("CUL_AND_MEM", "Culture & Memory", 2,
              ("Visual Technician", "Sculptor", "Cultural Archivist", "Manual Holder")),
    Committee("GOV_AND_LOG", "Governance & Logistics", 2,
              ("Settlement Governor", "Logistics High Command", "Biosphere Director", "Guardian of Humanity")),
    Committee("DEEP_SYS", "Deep Systems", 3,
              ("Quantum Engineer", "Quantum Physicist", "Neural Architect", "Sustenance Architect")),
    Committee("MEAN_AND_FRONT", "Meaning & Frontier", 3,
              ("Existential Expressionist", "Frontier Explorer", "Mission Seeker", "Colonel of Humanity")),
    Committee("FIELD_CONT", "Field Continuance", 3,
              ("Field Research Scientist", "Existential Chancellor", "Station Roamer", "Doctor")),
)
HumanKey = tuple[str, str]
AttributeSource = Literal["roster", "current", "simulated"]
AttributeKey = tuple[HumanKey, AttributeSource, str]
Position3D = tuple[float, float, float]
ATTRIBUTE_SOURCES: dict[AttributeSource, str] = {
    "roster": "Roster (raw)",
    "current": "Pod current (raw)",
    "simulated": "Pod simulated (raw)",
}


def asset_label(asset: str) -> str:
    name = asset.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    for prefix in ("DA_Profession_", "DA_Mutation_", "DA_Physical_Human_Property_", "DA_Human_Property_", "DA_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", " ")


def is_profession(asset: str) -> bool:
    return asset.startswith(PROFESSION_ROOT)


def profession_label(asset: str) -> str:
    return PROFESSIONS.get(asset, f"Unknown profession: {asset_label(asset)}")


@dataclass
class TraitSet:
    tag: gvas.PropertyTag
    chain: tuple[gvas.PropertyTag, ...]
    values: tuple[str, ...]
    encoded_values: tuple[bytes, ...]


@dataclass
class AttributeSet:
    tag: gvas.PropertyTag | None
    values: dict[str, float]
    offsets: dict[str, int]


@dataclass
class HumanPod:
    actor: str
    current: TraitSet
    simulated: TraitSet
    current_attributes: AttributeSet
    simulated_attributes: AttributeSet
    position: Position3D | None = None

    @property
    def attributes_current(self) -> dict[str, float]:
        return self.current_attributes.values

    @property
    def attributes_simulated(self) -> dict[str, float]:
        return self.simulated_attributes.values


@dataclass
class Human:
    world: str
    index: int
    social_id: str
    result: str
    traits: TraitSet
    attribute_set: AttributeSet
    timestamp: int | None = None
    delivery_timestamp: int | None = None
    pod: HumanPod | None = None
    edit_error: str = ""
    player_position: Position3D | None = None

    @property
    def key(self) -> HumanKey:
        return self.world, self.social_id

    @property
    def attributes(self) -> dict[str, float]:
        return self.attribute_set.values

    @property
    def attribute_sets(self) -> dict[AttributeSource, AttributeSet]:
        sites: dict[AttributeSource, AttributeSet] = {"roster": self.attribute_set}
        if self.pod is not None:
            sites["current"] = self.pod.current_attributes
            sites["simulated"] = self.pod.simulated_attributes
        return sites

    @property
    def in_space(self) -> bool:
        return self.result == "EVoyageResultType::Delivered"

    @property
    def location(self) -> str:
        if self.in_space:
            return "In space"
        if self.result == "EVoyageResultType::Success":
            return "In pod" if self.pod else "Grown (pod not found)"
        return self.result.rsplit("::", 1)[-1]

    @property
    def pod_distance_m(self) -> float | None:
        if self.pod is None or self.pod.position is None or self.player_position is None:
            return None
        return math.dist(self.pod.position, self.player_position) / 100

    @property
    def professions(self) -> tuple[str, ...]:
        return tuple(asset for asset in self.traits.values if is_profession(asset))

    @property
    def other_traits(self) -> tuple[str, ...]:
        return tuple(asset for asset in self.traits.values if not is_profession(asset))

    @property
    def trait_sets(self) -> tuple[TraitSet, ...]:
        if self.pod is not None:
            return self.traits, self.pod.current, self.pod.simulated
        return (self.traits,)

    @property
    def committees(self) -> tuple[str, ...]:
        return committees_for(self.professions)


def committees_for(professions: Iterable[str]) -> tuple[str, ...]:
    """Committees whose role list includes any of the given profession assets."""
    labels = {PROFESSIONS.get(asset) for asset in professions}
    return tuple(c.name for c in COMMITTEES if labels.intersection(c.professions))


def profession_differs(human: Human, profession: str | None) -> bool:
    """True when saving ``profession`` would change any roster or pod trait set."""
    wanted = (profession,) if profession is not None else ()
    return any(tuple(asset for asset in site.values if is_profession(asset)) != wanted
               for site in human.trait_sets)


@dataclass
class HumanData:
    humans: list[Human] = field(default_factory=list)
    worlds: list[str] = field(default_factory=list)
    committee_states: dict[tuple[str, str], str] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class CommitteeRole:
    world: str
    committee: Committee
    profession: str
    social_ids: tuple[str, ...]
    saved_state: str


def committee_coverage(
    data: HumanData, overrides: Mapping[HumanKey, str | None] | None = None,
) -> list[CommitteeRole]:
    """Profession matches only: not membership, age eligibility, or readiness.

    ``overrides`` maps a human to an unsaved replacement profession (``None``
    clears it) so the coverage can preview queued edits.
    """
    overrides = overrides or {}
    candidates: dict[tuple[str, str], list[str]] = defaultdict(list)
    for human in data.humans:
        if human.in_space:
            if human.key in overrides:
                profession = overrides[human.key]
                professions: tuple[str, ...] = (profession,) if profession is not None else ()
            else:
                professions = human.professions
            for asset in professions:
                candidates[human.world, asset].append(human.social_id)
    return [
        CommitteeRole(world, committee, label,
                      tuple(candidates[world, PROFESSION_ASSETS[label]]),
                      data.committee_states.get((world, committee.key), "Not serialized"))
        for world in data.worlds for committee in COMMITTEES for label in committee.professions
    ]


@overload
def _field(props: list[gvas.PropertyTag], name: str, *, required: Literal[True] = True) -> gvas.PropertyTag: ...


@overload
def _field(props: list[gvas.PropertyTag], name: str, *, required: Literal[False]) -> gvas.PropertyTag | None: ...


def _field(props: list[gvas.PropertyTag], name: str, *, required: bool = True) -> gvas.PropertyTag | None:
    matches = [tag for tag in props if tag.name == name]
    if len(matches) > 1 or (required and not matches):
        raise gvas.GvasError(f"Expected one {name} field; found {len(matches)}")
    return matches[0] if matches else None


def _bounded_string(raw: bytes, off: int, limit: int) -> tuple[str, int]:
    value, end = gvas.read_string(raw, off)
    count = struct.unpack_from("<i", raw, off)[0]
    terminator = 2 if count < 0 else 1
    if (end > limit or "\ufffd" in value
            or (count != 0 and raw[end - terminator:end] != b"\0" * terminator)):
        raise gvas.GvasError(f"Invalid human string at {off:#x}")
    return value, end


def _string(raw: bytes, tag: gvas.PropertyTag) -> str:
    if tag.type_name not in ("StrProperty", "NameProperty", "EnumProperty", "ObjectProperty"):
        raise gvas.GvasError(f"{tag.name} is not a string property")
    value, end = _bounded_string(raw, tag.payload, tag.end)
    if end != tag.end:
        raise gvas.GvasError(f"Invalid string in {tag.name}")
    return value


def _timestamp(raw: bytes, tag: gvas.PropertyTag | None) -> int | None:
    if tag is None:
        return None
    if tag.type_name != "Int64Property" or tag.size != 8:
        raise gvas.GvasError(f"Unsupported {tag.name} timestamp")
    return struct.unpack_from("<q", raw, tag.payload)[0]


def _map_entries(raw: bytes, tag: gvas.PropertyTag) -> list[gvas.MapEntry]:
    if tag.type_name != "MapProperty" or tag.size < 8:
        raise gvas.GvasError(f"Unsupported {tag.name} map")
    removed, count = struct.unpack_from("<ii", raw, tag.payload)
    if removed or not 0 <= count <= (tag.size - 8) // 4:
        raise gvas.GvasError(f"Unsupported removal list or count in {tag.name}")
    return gvas.parse_map(raw, tag, strict=True)[1]


def _struct_records(
    raw: bytes, tag: gvas.PropertyTag,
) -> tuple[list[gvas.MapEntry], tuple[gvas.PropertyTag, ...]]:
    if tag.type_name == "MapProperty":
        if len(tag.type.children) != 2 or tag.type.children[1].name != "StructProperty":
            raise gvas.GvasError(f"{tag.name} does not contain tagged structs")
        return _map_entries(raw, tag), (tag,)
    if (tag.type_name != "ArrayProperty" or tag.type.first is None
            or tag.type.first.name != "StructProperty" or tag.size < 4):
        raise gvas.GvasError(f"Unsupported {tag.name} struct array")
    count = struct.unpack_from("<i", raw, tag.payload)[0]
    if not 0 <= count <= (tag.size - 4) // 9:
        raise gvas.GvasError(f"Invalid {tag.name} array count")
    off, end = tag.payload + 4, tag.end
    chain = (tag,)
    if count:
        first, _ = gvas.read_tag(raw, off)
        # Older struct arrays have one element header spanning all records;
        # the current HumanGrow array instead starts directly with Type.
        if first is not None and first.name == tag.name and first.type_name == "StructProperty":
            if first.end != end:
                raise gvas.GvasError(f"Invalid element header in {tag.name}")
            off = first.payload
            chain += (first,)
    records = []
    for index in range(count):
        start = off
        props, off = gvas.read_property_list(raw, off)
        if off > end:
            raise gvas.GvasError(f"Record exceeds {tag.name}")
        records.append(gvas.MapEntry(index, start, off, index, start, props))
    if off != end:
        raise gvas.GvasError(f"{tag.name} count does not consume its payload")
    return records, chain


def _traits(raw: bytes, tag: gvas.PropertyTag, parents: tuple[gvas.PropertyTag, ...]) -> TraitSet:
    if (tag.type_name != "SetProperty" or tag.type.first is None
            or tag.type.first.name != "ObjectProperty" or tag.size < 8):
        raise gvas.GvasError(f"Unsupported {tag.name} trait set")
    removed, count = struct.unpack_from("<ii", raw, tag.payload)
    if removed or not 0 <= count <= (tag.size - 8) // 4:
        raise gvas.GvasError(f"Unsupported removal list or count in {tag.name}")
    off = tag.payload + 8
    values, encoded = [], []
    for _ in range(count):
        start = off
        value, off = _bounded_string(raw, off, tag.end)
        if not value or value in values:
            raise gvas.GvasError(f"Invalid or duplicate trait in {tag.name}")
        values.append(value)
        encoded.append(raw[start:off])
    if off != tag.end:
        raise gvas.GvasError(f"{tag.name} count does not consume its payload")
    return TraitSet(tag, parents + (tag,), tuple(values), tuple(encoded))


def _attributes(raw: bytes, tag: gvas.PropertyTag | None) -> AttributeSet:
    if tag is None:
        return AttributeSet(None, {}, {})
    if tuple(child.name for child in tag.type.children) != ("ObjectProperty", "DoubleProperty"):
        raise gvas.GvasError(f"Unsupported {tag.name} attributes")
    entries = _map_entries(raw, tag)
    values: dict[str, float] = {}
    offsets: dict[str, int] = {}
    for entry in entries:
        key, key_end = _bounded_string(raw, entry.start, entry.value_start)
        if not key or key != entry.key or key in values or key_end != entry.value_start:
            raise gvas.GvasError(f"Invalid or duplicate attribute in {tag.name}")
        if not tag.payload + 8 <= entry.value_start < entry.end <= tag.end or entry.end - entry.value_start != 8:
            raise gvas.GvasError(f"Invalid attribute bounds in {tag.name}")
        values[key] = struct.unpack_from("<d", raw, entry.value_start)[0]
        offsets[key] = entry.value_start
    return AttributeSet(tag, values, offsets)


def _blob_properties(raw: bytes, tag: gvas.PropertyTag) -> list[gvas.PropertyTag]:
    if (tag.type_name != "ArrayProperty" or tag.type.first is None
            or tag.type.first.name != "ByteProperty" or tag.size < 5
            or struct.unpack_from("<i", raw, tag.payload)[0] != tag.size - 4):
        raise gvas.GvasError("Unsupported human pod ByteData")
    # Voyage embeds a version byte before the tags and a four-byte trailer.
    for prefix in (0, 1):
        if prefix and raw[tag.payload + 4] != 0:
            continue
        try:
            props, end = gvas.read_property_list(raw, tag.payload + 4 + prefix)
        except (gvas.GvasError, struct.error):
            continue
        if (props and end <= tag.end and raw[end:tag.end] in (b"", b"\0" * 4)
                and any(prop.name == "HumanId" for prop in props)):
            return props
    raise gvas.GvasError("Human pod ByteData is not a complete supported property list")


def _has_human(raw: bytes, tag: gvas.PropertyTag) -> bool:
    if tag.type_name == "BoolProperty":
        if tag.size == 0:
            return bool(tag.flags & gvas.TAG_BOOL_TRUE)
        if tag.size == 1 and raw[tag.payload] in (0, 1):
            return bool(raw[tag.payload])
    raise gvas.GvasError("Unsupported bHasHumanCurrent value")


def _actor_position(raw: bytes, props: list[gvas.PropertyTag]) -> Position3D | None:
    transform = _field(props, "Transform", required=False)
    if (transform is None or transform.type_name != "StructProperty"
            or transform.type.first is None
            or transform.type.first.name != "VoyagePackedTransform"
            or transform.size != 36):
        return None
    position = struct.unpack_from("<3d", raw, transform.payload)
    return position if all(math.isfinite(value) for value in position) else None


def _read_pods(
    raw: bytes, world: str, world_props: list[gvas.PropertyTag],
    parents: tuple[gvas.PropertyTag, ...], humans: dict[HumanKey, Human],
) -> tuple[set[int], Position3D | None]:
    handled_ids: set[int] = set()
    player_positions: set[Position3D] = set()
    persistent = _field(world_props, "PersistentActors", required=False)
    if persistent is None:
        return handled_ids, None
    cells, cell_chain = _struct_records(raw, persistent)
    for cell in cells:
        actors_tag = _field(cell.props, "actors", required=False)
        if actors_tag is None:
            continue
        has_pod = raw.find(b"BP_Module_Human_Pod_", actors_tag.payload, actors_tag.end) >= 0
        has_player = raw.find(b"BP_FirstPersonCharacter", actors_tag.payload, actors_tag.end) >= 0
        if not has_pod and not has_player:
            continue
        actors, actor_chain = _struct_records(raw, actors_tag)
        for actor in actors:
            name_tag = _field(actor.props, "ActorName", required=False)
            if name_tag is None:
                continue
            name = _string(raw, name_tag)
            if name.startswith("BP_FirstPersonCharacter"):
                player_position = _actor_position(raw, actor.props)
                if player_position is not None:
                    player_positions.add(player_position)
                continue
            if not name.startswith("BP_Module_Human_Pod_"):
                continue
            pod_position = _actor_position(raw, actor.props)
            blob = _field(actor.props, "ByteData")
            props = _blob_properties(raw, blob)
            id_tag = _field(props, "HumanId")
            social_id = _string(raw, id_tag)
            handled_ids.add(id_tag.start)
            if not _has_human(raw, _field(props, "bHasHumanCurrent")):
                continue
            human = humans.get((world, social_id))
            if human is None:
                continue
            if human.pod is not None:
                human.edit_error = "More than one occupied pod has this Social ID."
                continue
            chain = parents + cell_chain + actor_chain + (blob,)
            human.pod = HumanPod(
                name,
                _traits(raw, _field(props, "TraitsCurrent"), chain),
                _traits(raw, _field(props, "TraitsSimulated"), chain),
                _attributes(raw, _field(props, "HumanPropertiesCurrent", required=False)),
                _attributes(raw, _field(props, "HumanPropertiesSimulated", required=False)),
                pod_position,
            )
    player_position = next(iter(player_positions)) if len(player_positions) == 1 else None
    return handled_ids, player_position


def _human_id_references(raw: bytes) -> Iterable[tuple[int, str]]:
    marker = struct.pack("<i", 8) + b"HumanId\0"
    off = raw.find(marker)
    while off >= 0:
        try:
            tag, _ = gvas.read_tag(raw, off)
            if tag is not None and tag.type_name == "StrProperty":
                yield off, _string(raw, tag)
        except (gvas.GvasError, struct.error):
            pass  # A marker in an unrelated string is not a property tag.
        off = raw.find(marker, off + len(marker))


def read_humans(raw: bytes) -> HumanData:
    """Read supported rosters and establish every trait set's exact ancestry."""
    data = HumanData()
    if struct.pack("<i", 10) + b"HumanGrow\0" not in raw:
        return data
    if struct.unpack_from("<I", raw, 0)[0] != len(raw) - 4:
        raise gvas.GvasError("Invalid save length envelope")
    root, _ = gvas.read_property_list(raw, gvas.find_root(raw))
    progress = _field(root, "GameProgress")
    if progress.type_name == "StructProperty":
        props, end = gvas.read_property_list(raw, progress.payload)
        if end != progress.end:
            raise gvas.GvasError("GameProgress does not consume its payload")
        worlds = [gvas.MapEntry(0, progress.payload, end, "World", progress.payload, props)]
    else:
        worlds, _ = _struct_records(raw, progress)
    by_key: dict[HumanKey, Human] = {}
    handled_ids: set[int] = set()
    seen_worlds: set[str] = set()
    for world_entry in worlds:
        if not isinstance(world_entry.key, str) or world_entry.key in seen_worlds:
            raise gvas.GvasError("Invalid or duplicate GameProgress world")
        seen_worlds.add(world_entry.key)
        roster = _field(world_entry.props, "HumanGrow", required=False)
        if roster is None:
            continue
        world = world_entry.key
        data.worlds.append(world)
        records, chain = _struct_records(raw, roster)
        for entry in records:
            social_id = _string(raw, _field(entry.props, "UniqueID"))
            if not social_id or (world, social_id) in by_key:
                raise gvas.GvasError(f"Empty or duplicate human Social ID in {world}")
            human = Human(
                world, entry.index + 1, social_id,
                _string(raw, _field(entry.props, "Type")),
                _traits(raw, _field(entry.props, "Traits"), (progress,) + chain),
                _attributes(raw, _field(entry.props, "properties", required=False)),
                _timestamp(raw, _field(entry.props, "Timestamp", required=False)),
                _timestamp(raw, _field(entry.props, "DeliveryTimestamp", required=False)),
            )
            if human.result not in ("EVoyageResultType::Delivered", "EVoyageResultType::Success"):
                human.edit_error = "Human editing is not supported for this result type."
            by_key[human.key] = human
            data.humans.append(human)
        committee_tag = _field(world_entry.props, "CommitteeData", required=False)
        if committee_tag is not None:
            committees, _ = _struct_records(raw, committee_tag)
            for entry in committees:
                key = (world, str(entry.key))
                if key in data.committee_states:
                    raise gvas.GvasError(f"Duplicate committee {key[1]}")
                data.committee_states[key] = _string(raw, _field(entry.props, "State"))
        world_humans = [human for human in data.humans if human.world == world]
        handled, player_position = _read_pods(
            raw, world, world_entry.props, (progress,), by_key,
        )
        handled_ids.update(handled)
        if player_position is not None:
            for human in world_humans:
                human.player_position = player_position
    if not data.worlds:
        raise gvas.GvasError("HumanGrow was not found in a supported GameProgress world")
    unlocated_ids = {value for off, value in _human_id_references(raw) if off not in handled_ids}
    for human in data.humans:
        if human.social_id in unlocated_ids:
            human.edit_error = "A matching HumanId is stored outside a supported pod; editing is disabled."
        elif human.result == "EVoyageResultType::Success" and human.pod is None:
            human.edit_error = "The occupied pod could not be located safely; editing is disabled."
    return data


def parse_attribute_value(value: object) -> float:
    """Raw stats are finite doubles, not inferred physical units or ages."""
    try:
        parsed = float(str(value).strip())
    except (ValueError, OverflowError) as exc:
        raise ValueError("Enter a finite number for the raw stat value.") from exc
    if not math.isfinite(parsed):
        raise ValueError("Enter a finite number for the raw stat value; NaN and infinity are not supported.")
    return parsed


def apply_attributes(raw: bytes, changes: Mapping[AttributeKey, object]) -> tuple[bytes, int]:
    """Patch existing roster/pod doubles independently; never add or resize maps."""
    if not changes:
        return raw, 0
    humans = {human.key: human for human in read_humans(raw).humans}
    replacements: dict[AttributeKey, tuple[int, bytes]] = {}
    for key, value in changes.items():
        human_key, source, asset = key
        if human_key not in humans:
            raise ValueError("A selected human is no longer in this save; reload and select again")
        human = humans[human_key]
        if human.edit_error:
            raise gvas.GvasError(f"{human.social_id}: {human.edit_error}")
        site = human.attribute_sets.get(source)
        if site is None or asset not in site.offsets:
            raise ValueError(f"{human.social_id}: the selected raw stat is no longer available; reload the save")
        encoded = struct.pack("<d", parse_attribute_value(value))
        offset = site.offsets[asset]
        if raw[offset:offset + 8] != encoded:
            replacements[key] = offset, encoded
    if not replacements:
        return raw, 0
    buf = bytearray(raw)
    for offset, encoded in replacements.values():
        buf[offset:offset + 8] = encoded
    result = bytes(buf)
    verified = {human.key: human for human in read_humans(result).humans}
    if verified.keys() != humans.keys():
        raise gvas.GvasError("Human roster changed during stat verification")
    for (human_key, source, asset), (offset, encoded) in replacements.items():
        site = verified[human_key].attribute_sets.get(source)
        if (site is None or site.offsets.get(asset) != offset
                or struct.pack("<d", site.values[asset]) != encoded):
            raise gvas.GvasError("Human stat verification failed")
    return result, len(replacements)


def replace_professions(
    raw: bytes, keys: Iterable[HumanKey], profession: str | None,
) -> tuple[bytes, int]:
    """Replace profession traits only, preserving all other serialized fields."""
    if profession is not None and profession not in PROFESSIONS:
        raise ValueError("Choose a profession from the supported v6 catalogue")
    return apply_professions(raw, dict.fromkeys(keys, profession))


def apply_professions(
    raw: bytes, changes: Mapping[HumanKey, str | None],
) -> tuple[bytes, int]:
    """Give each listed human its own replacement profession in one rewrite.

    ``None`` clears every profession. Only profession traits change; mutations,
    other traits and every unrelated byte are preserved and re-verified.
    """
    if any(profession is not None and profession not in PROFESSIONS for profession in changes.values()):
        raise ValueError("Choose a profession from the supported v6 catalogue")
    if not changes:
        return raw, 0
    data = read_humans(raw)
    humans = {human.key: human for human in data.humans}
    if changes.keys() - humans.keys():
        raise ValueError("A selected human is no longer in this save; reload and select again")
    wanted = {key: (profession,) if profession is not None else () for key, profession in changes.items()}
    replacements: list[tuple[TraitSet, bytes]] = []
    changed: set[HumanKey] = set()
    for key in sorted(changes):
        human = humans[key]
        if human.edit_error:
            raise gvas.GvasError(f"{human.social_id}: {human.edit_error}")
        for site in human.trait_sets:
            if tuple(asset for asset in site.values if is_profession(asset)) == wanted[key]:
                continue
            kept = [encoded for asset, encoded in zip(site.values, site.encoded_values)
                    if not is_profession(asset)]
            for asset in wanted[key]:
                encoded = asset.encode("utf-8") + b"\0"
                kept.append(struct.pack("<i", len(encoded)) + encoded)
            payload = struct.pack("<ii", 0, len(kept)) + b"".join(kept)
            replacements.append((site, payload))
            changed.add(key)
    if not replacements:
        return raw, 0
    replacements.sort(key=lambda pair: pair[0].tag.payload)
    deltas: dict[int, int] = defaultdict(int)
    previous_end = 0
    for site, payload in replacements:
        if site.tag.payload < previous_end:
            raise gvas.GvasError("Overlapping human trait sets")
        previous_end = site.tag.end
        delta = len(payload) - site.tag.size
        for ancestor in site.chain:
            if not ancestor.payload <= site.tag.payload <= site.tag.end <= ancestor.end:
                raise gvas.GvasError("Invalid human trait ancestor")
            deltas[ancestor.size_offset] += delta
            if (ancestor.type_name == "ArrayProperty" and ancestor.type.first is not None
                    and ancestor.type.first.name == "ByteProperty"):
                deltas[ancestor.payload] += delta
    buf = bytearray(raw)
    for offset, delta in deltas.items():
        value = struct.unpack_from("<i", raw, offset)[0] + delta
        if not 0 <= value <= 0x7FFFFFFF:
            raise gvas.GvasError("Human edit exceeds a container's size range")
        struct.pack_into("<i", buf, offset, value)
    for site, payload in reversed(replacements):
        buf[site.tag.payload:site.tag.end] = payload
    struct.pack_into("<I", buf, 0, len(buf) - 4)
    result = bytes(buf)
    verified = {human.key: human for human in read_humans(result).humans}
    if verified.keys() != humans.keys():
        raise gvas.GvasError("Human roster changed during verification")
    for key, before in humans.items():
        after = verified[key]
        if len(before.trait_sets) != len(after.trait_sets):
            raise gvas.GvasError("Human pod linkage changed during verification")
        for old, new in zip(before.trait_sets, after.trait_sets):
            old_others = tuple(blob for asset, blob in zip(old.values, old.encoded_values)
                               if not is_profession(asset))
            new_others = tuple(blob for asset, blob in zip(new.values, new.encoded_values)
                               if not is_profession(asset))
            expected = (wanted[key] if key in wanted
                        else tuple(a for a in old.values if is_profession(a)))
            if old_others != new_others or tuple(a for a in new.values if is_profession(a)) != expected:
                raise gvas.GvasError("Human trait verification failed")
    return result, len(changed)
