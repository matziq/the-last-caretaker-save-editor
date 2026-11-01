"""Structural GVAS parser for The Last Caretaker (Voyage) saves.

The scalar editor in :mod:`last_caretaker_save_edit` only performs same-width
in-place swaps, which can never change the length of the payload.  Truly
removing an inventory entry requires deleting bytes, which means every
enclosing container's ``Size`` header has to shrink by the same amount.

This module implements just enough of Voyage's property serialization to walk
from the save root down to a specific offset, collect that ancestor chain and
splice bytes out safely.

Voyage's property tag layout is *not* the classic Unreal one.  It is::

    Name        FString
    TypeTree    FString type name + int32 child count + that many child nodes
    Size        int32          (length of the payload that follows HasGuid)
    HasGuid     uint8          (+ 16 byte GUID when set)
    Payload     Size bytes

A ``MapProperty`` payload is ``int32 NumKeysToRemove``, ``int32 Num`` and then
``Num`` untagged key/value pairs.
"""

from __future__ import annotations

import hashlib
import struct
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

__all__ = [
    "GvasError",
    "TypeNode",
    "PropertyTag",
    "MapEntry",
    "ItemMap",
    "read_string",
    "read_type_node",
    "read_tag",
    "read_property_list",
    "find_root",
    "parse_map",
    "find_path",
    "find_item_maps",
    "entry_values",
    "entry_field_offsets",
    "remove_map_entries",
    "purge_map_entries",
]


class GvasError(Exception):
    """Raised when the payload does not match the expected structure."""


@dataclass(frozen=True)
class TypeNode:
    name: str
    children: Tuple["TypeNode", ...] = ()

    @property
    def first(self) -> Optional["TypeNode"]:
        return self.children[0] if self.children else None


@dataclass
class PropertyTag:
    name: str
    type: TypeNode
    size: int
    size_offset: int
    start: int
    payload: int
    end: int
    flags: int = 0

    @property
    def type_name(self) -> str:
        return self.type.name


@dataclass
class MapEntry:
    index: int
    start: int
    end: int
    key: object
    value_start: int
    props: List[PropertyTag] = field(default_factory=list)


_NONE = "None"
_MAX_STRING = 1 << 20

# UE 5.4+ EPropertyTagFlags, written as a single byte after the tag's Size.
TAG_HAS_ARRAY_INDEX = 0x01
TAG_HAS_PROPERTY_GUID = 0x02
TAG_HAS_PROPERTY_EXTENSIONS = 0x04
TAG_SERIALIZE_AS_ZERO = 0x08
TAG_BOOL_TRUE = 0x10
_TAG_KNOWN_FLAGS = 0x1F

# EPropertyTagExtension
EXT_OVERRIDABLE_INFORMATION = 0x01


def read_string(raw: bytes, off: int, *, require_terminator: bool = False) -> Tuple[str, int]:
    """Read an Unreal FString.  Handles ASCII and UTF-16 (negative length)."""
    if off < 0 or off + 4 > len(raw):
        raise GvasError(f"string length out of range at {off}")
    (count,) = struct.unpack_from("<i", raw, off)
    off += 4
    if count == 0:
        return "", off
    if count > 0:
        if count > _MAX_STRING or off + count > len(raw):
            raise GvasError(f"implausible string length {count} at {off - 4}")
        if require_terminator and raw[off + count - 1] != 0:
            raise GvasError(f"unterminated string at {off - 4}")
        return raw[off:off + count - 1].decode("utf-8", "replace"), off + count
    size = -count * 2
    if size > _MAX_STRING or off + size > len(raw):
        raise GvasError(f"implausible utf16 length {count} at {off - 4}")
    if require_terminator and raw[off + size - 2:off + size] != b"\0\0":
        raise GvasError(f"unterminated utf16 string at {off - 4}")
    return raw[off:off + size - 2].decode("utf-16-le", "replace"), off + size


def read_type_node(
    raw: bytes, off: int, depth: int = 0, *, require_terminator: bool = False,
) -> Tuple[TypeNode, int]:
    if depth > 8:
        raise GvasError("type tree too deep")
    name, off = read_string(raw, off, require_terminator=require_terminator)
    if off + 4 > len(raw):
        raise GvasError(f"type child count out of range at {off}")
    (count,) = struct.unpack_from("<i", raw, off)
    off += 4
    if not 0 <= count <= 8:
        raise GvasError(f"implausible type child count {count} at {off - 4}")
    children = []
    for _ in range(count):
        node, off = read_type_node(raw, off, depth + 1, require_terminator=require_terminator)
        children.append(node)
    return TypeNode(name, tuple(children)), off


def read_tag(
    raw: bytes, off: int, *, require_terminator: bool = False,
) -> Tuple[Optional[PropertyTag], int]:
    """Read one property tag.  Returns ``(None, end)`` for the ``None`` terminator."""
    start = off
    name, off = read_string(raw, off, require_terminator=require_terminator)
    if name == _NONE:
        return None, off
    node, off = read_type_node(raw, off, require_terminator=require_terminator)
    if off + 5 > len(raw):
        raise GvasError(f"tag header out of range at {off}")
    (size,) = struct.unpack_from("<i", raw, off)
    size_offset = off
    off += 4
    flags = raw[off]
    off += 1
    if flags & ~_TAG_KNOWN_FLAGS:
        raise GvasError(f"unknown property tag flags {flags:#04x} for {name!r} at {off - 1}")
    if flags & TAG_HAS_ARRAY_INDEX:
        off += 4
    if flags & TAG_HAS_PROPERTY_GUID:
        off += 16
    if flags & TAG_HAS_PROPERTY_EXTENSIONS:
        if off >= len(raw):
            raise GvasError(f"tag extension out of range at {off}")
        ext = raw[off]
        off += 1
        if ext & ~EXT_OVERRIDABLE_INFORMATION:
            raise GvasError(f"unknown tag extension {ext:#04x} for {name!r}")
        if ext & EXT_OVERRIDABLE_INFORMATION:
            off += 2  # override operation + bShouldReferenceBeMappedToNull
    if size < 0 or off + size > len(raw):
        raise GvasError(f"implausible property size {size} for {name!r} at {size_offset}")
    return PropertyTag(name, node, size, size_offset, start, off, off + size, flags), off


def read_property_list(raw: bytes, off: int, limit: int = 4096) -> Tuple[List[PropertyTag], int]:
    """Read tagged properties until the ``None`` terminator."""
    out: List[PropertyTag] = []
    while True:
        if len(out) > limit:
            raise GvasError(f"property list exceeded {limit} entries at {off}")
        tag, after = read_tag(raw, off)
        if tag is None:
            return out, after
        out.append(tag)
        off = tag.end


# --------------------------------------------------------------------------
# untagged values (map keys / values)
# --------------------------------------------------------------------------

_FIXED_WIDTH = {
    "IntProperty": 4,
    "UInt32Property": 4,
    "Int32Property": 4,
    "FloatProperty": 4,
    "Int64Property": 8,
    "UInt64Property": 8,
    "DoubleProperty": 8,
    "BoolProperty": 1,
    "ByteProperty": 1,
}

_STRING_TYPES = {"StrProperty", "NameProperty", "EnumProperty", "ObjectProperty"}


def read_untagged(raw: bytes, off: int, node: TypeNode) -> Tuple[object, int]:
    """Read a map key or value, which carries no tag of its own."""
    name = node.name
    width = _FIXED_WIDTH.get(name)
    if width is not None:
        fmt = {4: "<i", 8: "<q", 1: "<B"}[width]
        if name == "FloatProperty":
            fmt = "<f"
        elif name == "DoubleProperty":
            fmt = "<d"
        elif name == "UInt32Property":
            fmt = "<I"
        (value,) = struct.unpack_from(fmt, raw, off)
        return value, off + width
    if name in _STRING_TYPES:
        return read_string(raw, off)
    if name == "SoftObjectProperty":
        first, off = read_string(raw, off)
        second, off = read_string(raw, off)
        return (first, second), off
    if name == "StructProperty":
        props, off = read_property_list(raw, off)
        return props, off
    raise GvasError(f"unsupported untagged type {name!r} at {off}")


def parse_map(
    raw: bytes, tag: PropertyTag, *, strict: bool = True
) -> Tuple[int, List[MapEntry]]:
    """Parse a MapProperty payload.  Returns ``(num_keys_to_remove, entries)``.

    Some entry value structs use field layouts this parser does not fully
    understand yet (e.g. an uncommon nested struct type). When ``strict`` is
    ``True`` (the default, used whenever a caller already knows exactly which
    map/offset it wants) any such failure raises, matching prior behaviour.

    When ``strict`` is ``False`` (used by :func:`find_item_maps`'s exploratory
    sweep) a failure while decoding one entry stops the scan for *this* map
    but still returns every entry successfully parsed *before* it, instead of
    discarding the whole map. This matters because a single unusual entry
    partway through a large ``Items`` map must not hide every genuine,
    perfectly-parseable duplicate stack that comes before it.
    """
    if tag.type_name != "MapProperty":
        raise GvasError(f"{tag.name!r} is a {tag.type_name}, not a MapProperty")
    if len(tag.type.children) != 2:
        raise GvasError(f"map {tag.name!r} has {len(tag.type.children)} inner types")
    key_type, value_type = tag.type.children
    num_remove, num = struct.unpack_from("<ii", raw, tag.payload)
    off = tag.payload + 8
    entries: List[MapEntry] = []
    for index in range(num):
        start = off
        try:
            key, off = read_untagged(raw, off, key_type)
            value_start = off
            value, off = read_untagged(raw, off, value_type)
        except (GvasError, struct.error):
            if strict:
                raise
            break
        entry = MapEntry(index, start, off, key, value_start)
        if isinstance(value, list):
            entry.props = value
        entries.append(entry)
    if strict and off != tag.end:
        raise GvasError(
            f"map {tag.name!r} consumed {off - tag.payload} bytes, expected {tag.size}")
    return num_remove, entries


# --------------------------------------------------------------------------
# root + path finding
# --------------------------------------------------------------------------

def find_root(raw: bytes) -> int:
    """Return the offset of the save's first property tag."""
    off = 4  # outer uint32 length prefix
    if raw[off:off + 4] != b"GVAS":
        raise GvasError("payload does not start with GVAS")
    off += 4
    off += 12  # save game version + package versions (UE4 + UE5)
    off += 2 + 2 + 2 + 4  # engine version major/minor/patch + changelist
    _branch, off = read_string(raw, off)
    off += 4  # custom version format
    (count,) = struct.unpack_from("<i", raw, off)
    off += 4
    if not 0 <= count <= 4096:
        raise GvasError(f"implausible custom version count {count}")
    off += count * 20
    _cls, off = read_string(raw, off)
    # A shifted read can decode a few stray bytes as a huge string length and
    # still yield a tag that looks plausible, so a candidate has to read as
    # printable names and parse as a complete top-level list to be trusted.
    fallback: Optional[int] = None
    for pad in range(0, 9):
        try:
            tag, _ = read_tag(raw, off + pad)
        except GvasError:
            continue
        if tag is None or not tag.name or not tag.type_name.endswith("Property"):
            continue
        if _is_plain_name(tag.name) and _is_plain_name(tag.type_name) and _lists_cleanly(raw, off + pad):
            return off + pad
        if fallback is None:
            fallback = off + pad
    if fallback is not None:
        return fallback
    raise GvasError("could not locate the root property list")


def _is_plain_name(text: str) -> bool:
    return bool(text) and text.isascii() and text.isprintable()


def _lists_cleanly(raw: bytes, start: int) -> bool:
    """True when the property list at ``start`` ends on its ``None`` terminator."""
    try:
        read_property_list(raw, start)
    except GvasError:
        return False
    return True


def find_path(raw: bytes, target: int, root: Optional[int] = None) -> List[PropertyTag]:
    """Return every property tag whose payload encloses ``target``, outermost first.

    The returned chain is what has to shrink when bytes are spliced out at
    ``target``.
    """
    if root is None:
        root = find_root(raw)
    chain: List[PropertyTag] = []
    off = root
    while True:
        tag, after = read_tag(raw, off)
        if tag is None:
            raise GvasError(f"offset {target} not reachable from {off}")
        if tag.start == target:
            chain.append(tag)
            return chain
        if not (tag.payload <= target < tag.end):
            off = tag.end
            continue
        chain.append(tag)
        if tag.payload == target:
            return chain
        kind = tag.type_name
        if kind == "StructProperty":
            off = tag.payload
            continue
        if kind == "MapProperty":
            _num_remove, entries = parse_map(raw, tag)
            for entry in entries:
                if entry.value_start <= target < entry.end:
                    off = entry.value_start
                    break
            else:
                raise GvasError(f"offset {target} not inside an entry of {tag.name!r}")
            continue
        if kind == "ArrayProperty":
            inner = tag.type.first
            inner_name = inner.name if inner else "?"
            if inner_name == "ByteProperty":
                # A raw serialized component blob: int32 byte count, then an
                # optional leading version byte, then an ordinary property list.
                blob = tag.payload + 4
                for pad in range(0, 9):
                    try:
                        probe, _ = read_tag(raw, blob + pad)
                    except GvasError:
                        continue
                    if probe is not None and probe.type_name.endswith("Property"):
                        off = blob + pad
                        break
                else:
                    raise GvasError(f"could not find a property list inside {tag.name!r}")
                continue
            if inner_name != "StructProperty":
                raise GvasError(
                    f"offset {target} sits inside opaque "
                    f"ArrayProperty<{inner_name}> {tag.name!r}")
            (num,) = struct.unpack_from("<i", raw, tag.payload)
            cur = tag.payload + 4
            header, cur = read_tag(raw, cur)
            if header is None:
                raise GvasError(f"array {tag.name!r} has no element header")
            chain.append(header)
            for _ in range(num):
                start = cur
                _props, cur = read_property_list(raw, start)
                if start <= target < cur:
                    off = start
                    break
            else:
                raise GvasError(f"offset {target} not inside an element of {tag.name!r}")
            continue
        raise GvasError(f"cannot descend into {kind} {tag.name!r}")


def entry_values(raw: bytes, entry: MapEntry) -> dict:
    """Flatten an entry's struct properties into ``{"Data.ItemCount": 3, ...}``."""
    return _flatten(raw, entry.props, "", {})


def entry_field_offsets(raw: bytes, entry: MapEntry) -> dict:
    """Map an entry's flattened property names to their payload offsets."""
    out: dict = {}

    def walk(props: Sequence[PropertyTag], prefix: str) -> None:
        for tag in props:
            key = prefix + tag.name
            if tag.type_name == "StructProperty":
                try:
                    sub, _ = read_property_list(raw, tag.payload)
                except GvasError:
                    continue
                walk(sub, key + ".")
            else:
                out[key] = tag.payload

    walk(entry.props, "")
    return out


def _flatten(raw: bytes, props: Sequence[PropertyTag], prefix: str, out: dict) -> dict:
    for tag in props:
        key = prefix + tag.name
        kind = tag.type_name
        try:
            if kind == "StructProperty":
                sub, _ = read_property_list(raw, tag.payload)
                _flatten(raw, sub, key + ".", out)
            elif kind in ("NameProperty", "StrProperty", "EnumProperty"):
                out[key] = read_string(raw, tag.payload)[0]
            elif kind in ("IntProperty", "UInt32Property"):
                out[key] = struct.unpack_from("<i", raw, tag.payload)[0]
            elif kind == "Int64Property":
                out[key] = struct.unpack_from("<q", raw, tag.payload)[0]
            elif kind == "FloatProperty":
                out[key] = struct.unpack_from("<f", raw, tag.payload)[0]
            elif kind == "ByteProperty":
                out[key] = raw[tag.payload]
        except (GvasError, struct.error):
            continue
    return out


def read_text_key(raw: bytes, tag: PropertyTag) -> Optional[str]:
    """Return the string-table key of a ``TextProperty``.

    FText serializes as ``int32 Flags``, ``int8 HistoryType`` and then a run of
    FStrings.  For string-table entries the run is the table id followed by the
    key, so the last non-empty string is the one worth showing.
    """
    off = tag.payload + 5
    last: Optional[str] = None
    while off < tag.end:
        try:
            value, nxt = read_string(raw, off)
        except GvasError:
            break
        if nxt > tag.end:
            break
        if value:
            last = value
        off = nxt
    return last


@dataclass
class ItemMap:
    """An ``Items`` map together with everything that has to shrink around it.

    ``owner`` is the key of the nearest string-keyed map entry (the world
    partition cell, or ``pawns``); ``actor`` is the ``ActorName`` of the
    persistent actor that serialized the map.
    """

    tag: PropertyTag
    chain: List[PropertyTag]
    entries: List[MapEntry]
    owner: str
    label: Optional[str] = None
    is_module: bool = False
    actor: str = ""

    @property
    def start(self) -> int:
        return self.tag.start


_MAP_CACHE: "OrderedDict[tuple, List[ItemMap]]" = OrderedDict()
# Distinct save contents whose walks are kept. Every walk is ~100-160 MB on a
# large save, and one Repair & Top Off produces a new content per step.
_MAP_CACHE_CONTENTS = 2
_DIGEST_MEMO: Optional[Tuple[bytes, bytes]] = None


def _digest(raw: bytes) -> bytes:
    """Content digest of ``raw`` (about 0.15 s for a 150 MB save).

    The latest buffer is remembered by identity, which is exact because bytes
    are immutable, so the many lookups made while one save is open hash it
    once. Older buffers are not kept alive.
    """
    global _DIGEST_MEMO
    memo = _DIGEST_MEMO
    if memo is not None and memo[0] is raw:
        return memo[1]
    digest = hashlib.blake2b(raw, digest_size=16).digest()
    _DIGEST_MEMO = (raw, digest)
    return digest


def _cache_key(raw: bytes, root: Optional[int], name: str) -> Optional[tuple]:
    # Keyed by content digest rather than by the buffer itself, which would
    # keep every cached save's 150 MB alive. A reloaded copy of the same save
    # still hits. Mutable buffers are never cached.
    if not isinstance(raw, bytes):
        return None
    return (len(raw), _digest(raw), root, name)


def _trim_item_map_cache() -> None:
    """Keep only the most recently used saves' walks."""
    keep: List[tuple] = []
    for key in reversed(_MAP_CACHE):
        if key[:2] not in keep:
            keep.append(key[:2])
    keep = keep[:_MAP_CACHE_CONTENTS]
    for key in list(_MAP_CACHE):
        if key[:2] not in keep:
            del _MAP_CACHE[key]


def carry_item_map_cache(old_raw: bytes, new_raw: bytes) -> int:
    """Reuse cached walks for ``new_raw`` when it differs only in scalar bytes.

    Only valid for same-width value writes that leave every tag, size header
    and map count untouched (and no BoolProperty, whose value lives in the tag
    flags). Returns the number of cached walks carried over.
    """
    if not isinstance(old_raw, bytes) or not isinstance(new_raw, bytes) or len(old_raw) != len(new_raw):
        return 0
    old_content = (len(old_raw), _digest(old_raw))
    new_content = (len(new_raw), _digest(new_raw))
    carried = 0
    for key, maps in list(_MAP_CACHE.items()):
        if key[:2] == old_content:
            _MAP_CACHE[new_content + key[2:]] = maps
            carried += 1
    _trim_item_map_cache()
    return carried


def clear_item_map_cache() -> None:
    global _DIGEST_MEMO
    _MAP_CACHE.clear()
    _DIGEST_MEMO = None


def find_item_maps(
    raw: bytes,
    *,
    root: Optional[int] = None,
    name: str = "Items",
) -> List[ItemMap]:
    """Walk the whole save once and collect every map called ``name``.

    The walk takes tens of seconds on a large save, so results are cached by
    save content (the returned structures hold offsets only).
    """
    key = _cache_key(raw, root, name)
    if key is not None and key in _MAP_CACHE:
        _MAP_CACHE.move_to_end(key)
        return list(_MAP_CACHE[key])
    # ``Attachments`` maps live inside ``Items`` entries, so one walk that
    # records both costs the same as the ``Attachments`` walk alone.
    names = _SHARED_WALK if name in _SHARED_WALK else (name,)
    results = _walk_item_maps(raw, root=root, names=names)
    if key is not None:
        for walked, found in results.items():
            _MAP_CACHE[key[:2] + (root, walked)] = found
        _trim_item_map_cache()
    return list(results[name])


_SHARED_WALK = ("Items", "Attachments")
_ACTOR_NAME = "ActorName"


def _read_name(raw: bytes, tag: PropertyTag) -> str:
    """Read a ``NameProperty`` payload, which is a plain FString here."""
    try:
        return read_string(raw, tag.payload)[0]
    except GvasError:
        return ""


def _walk_item_maps(
    raw: bytes,
    *,
    root: Optional[int] = None,
    names: Sequence[str] = ("Items",),
) -> dict:
    """Collect maps for each of ``names`` in one walk.

    For every name the result equals a walk for that name alone: a map is
    recorded unless an enclosing map already has the same name (a single-name
    walk stops at the first match and never sees nested namesakes).
    """
    if root is None:
        root = find_root(raw)
    results: dict = {walked: [] for walked in names}
    single = len(names) == 1

    def visit(
        off: int,
        chain: List[PropertyTag],
        owner: str,
        bound: Optional[int] = None,
        actor: str = "",
    ) -> None:
        siblings: List[PropertyTag] = []
        while True:
            if bound is not None and off >= bound:
                # No ``None`` terminator turned up before the enclosing
                # container's own declared end -- the probed offset was
                # wrong (or the blob isn't a plain property list). Abandon
                # this branch instead of reading into unrelated data.
                return
            try:
                tag, _after = read_tag(raw, off, require_terminator=True)
            except GvasError:
                return
            if tag is None:
                break
            if bound is not None and tag.end > bound:
                return
            siblings.append(tag)
            off = tag.end
        visit_parsed(siblings, chain, owner, bound, actor)

    def visit_parsed(
        siblings: Sequence[PropertyTag],
        chain: List[PropertyTag],
        owner: str,
        bound: Optional[int],
        actor: str = "",
    ) -> None:
        if bound is not None and any(tag.end > bound for tag in siblings):
            return
        for tag in siblings:
            if tag.name == _ACTOR_NAME and tag.type_name == "NameProperty":
                actor = _read_name(raw, tag) or actor
                break
        for tag in siblings:
            descend(tag, chain, owner, siblings, actor)

    def descend(
        tag: PropertyTag,
        chain: List[PropertyTag],
        owner: str,
        siblings: Sequence[PropertyTag] = (),
        actor: str = "",
    ) -> None:
        kind = tag.type_name
        sub_chain = chain + [tag]
        if kind == "StructProperty":
            visit(tag.payload, sub_chain, owner, tag.end, actor)
            return
        if kind == "MapProperty":
            try:
                _num_remove, entries = parse_map(raw, tag, strict=False)
            except (GvasError, struct.error):
                return
            if tag.name in results and not any(
                ancestor.name == tag.name and ancestor.type_name == "MapProperty"
                for ancestor in chain
            ):
                label = None
                is_module = False
                for sibling in siblings:
                    if sibling.name == "Name" and sibling.type_name == "TextProperty":
                        label = read_text_key(raw, sibling)
                    elif sibling.name == "ModuleState":
                        is_module = True
                results[tag.name].append(
                    ItemMap(tag, sub_chain, entries, owner, label, is_module, actor)
                )
                if single:
                    return
            value_type = tag.type.children[1] if len(tag.type.children) == 2 else None
            if value_type is None or value_type.name != "StructProperty":
                return
            for entry in entries:
                key_owner = entry.key if isinstance(entry.key, str) else owner
                # parse_map already decoded these property tags.
                visit_parsed(entry.props, sub_chain, key_owner, entry.end, actor)
            return
        if kind == "ArrayProperty":
            inner = tag.type.first
            inner_name = inner.name if inner else ""
            if inner_name == "ByteProperty":
                blob = tag.payload + 4
                for pad in range(0, 9):
                    try:
                        probe, _ = read_tag(raw, blob + pad, require_terminator=True)
                    except GvasError:
                        continue
                    if probe is not None and probe.type_name.endswith("Property") and probe.end <= tag.end:
                        visit(blob + pad, sub_chain, owner, tag.end, actor)
                        return
                return
            if inner_name != "StructProperty":
                return
            try:
                (num,) = struct.unpack_from("<i", raw, tag.payload)
                cur = tag.payload + 4
                header, cur = read_tag(raw, cur)
            except (GvasError, struct.error):
                return
            if header is None:
                return
            elem_chain = sub_chain + [header]
            for _ in range(num):
                try:
                    _props, nxt = read_property_list(raw, cur)
                except GvasError:
                    return
                if nxt > tag.end:
                    return
                visit_parsed(_props, elem_chain, owner, tag.end, actor)
                cur = nxt
            return

    visit(root, [], "")
    return results


def remove_map_entries(
    raw: bytes,
    map_start: int,
    should_remove: Callable[[MapEntry], bool],
    *,
    root: Optional[int] = None,
    item_map: Optional[ItemMap] = None,
) -> Tuple[bytes, int]:
    """Delete matching entries from the map whose tag begins at ``map_start``.

    Returns the rewritten payload and the number of entries removed.  Every
    enclosing container's ``Size`` header, any raw byte-blob element count and
    the outer length prefix are adjusted so the result stays loadable.

    Pass ``item_map`` (from :func:`find_item_maps`) to reuse an already parsed
    chain instead of walking the save again.
    """
    if item_map is not None:
        chain, tag, entries = item_map.chain, item_map.tag, item_map.entries
    else:
        chain = find_path(raw, map_start, root=root)
        tag = chain[-1]
        _num_remove, entries = parse_map(raw, tag)
    if tag.start != map_start:
        raise GvasError(f"no property tag starts at {map_start}")
    if tag.type_name != "MapProperty":
        raise GvasError(f"{tag.name!r} is a {tag.type_name}, not a MapProperty")

    # Read the map's own declared entry count directly rather than trusting
    # ``len(entries)``: when ``entries`` came from a best-effort scan (see
    # ``parse_map``'s ``strict`` flag) it may only cover a prefix of the real
    # entries, and ``kept`` must still reflect every entry that stays behind,
    # not just the ones this parser understood well enough to enumerate.
    num_remove, num_total = struct.unpack_from("<ii", raw, tag.payload)

    # Refuse to touch a map this parser could not fully account for. If
    # ``entries`` is a best-effort prefix (shorter than the map's own
    # declared entry count) then every entry past that point is still
    # physically present in the file but invisible to us -- splicing bytes
    # out and rewriting the declared count to something shorter than
    # ``num_total`` would silently orphan that trailing data. The game's own
    # loader then reads exactly ``num_total`` (or whatever new, wrong count
    # we wrote) entries and, on hitting the *real* boundary of what actually
    # remains, misreads leftover entry bytes as a brand new property tag --
    # this is a genuine, previously-observed source of save corruption, not
    # a theoretical concern.
    if len(entries) < num_total:
        raise GvasError(
            f"map {tag.name!r} declares {num_total} entries but only "
            f"{len(entries)} were parseable; refusing to remove entries from "
            "a map this parser cannot fully account for"
        )

    doomed = [entry for entry in entries if should_remove(entry)]
    if not doomed:
        return bytes(raw), 0

    cuts = [(entry.start, entry.end) for entry in doomed]
    removed_bytes = sum(end - start for start, end in cuts)
    kept = num_total - len(doomed)

    buf = bytearray(raw)
    # Shrink every enclosing Size header (the map itself is chain[-1]).
    for ancestor in chain:
        (old,) = struct.unpack_from("<i", buf, ancestor.size_offset)
        struct.pack_into("<i", buf, ancestor.size_offset, old - removed_bytes)
        # A raw byte blob also stores its own element count.
        if (ancestor.type_name == "ArrayProperty"
                and ancestor.type.first is not None
                and ancestor.type.first.name == "ByteProperty"):
            (count,) = struct.unpack_from("<i", buf, ancestor.payload)
            struct.pack_into("<i", buf, ancestor.payload, count - removed_bytes)
    struct.pack_into("<ii", buf, tag.payload, num_remove, kept)
    # Splice from the end so earlier offsets stay valid.
    for start, end in sorted(cuts, reverse=True):
        del buf[start:end]
    # The payload opens with a uint32 length prefix covering everything after it.
    struct.pack_into("<I", buf, 0, len(buf) - 4)
    return bytes(buf), len(doomed)


def append_map_entries(
    raw: bytes,
    map_start: int,
    blobs: Sequence[bytes],
    *,
    root: Optional[int] = None,
) -> bytes:
    """Append fully serialized ``key + value`` entries to a map.

    The map must parse completely (strictly), every appended blob must parse
    as exactly one entry of the map's key/value types, and afterwards the map
    is re-parsed strictly so a malformed insertion can never be written out.
    Every enclosing ``Size`` header, raw byte-blob element count and the outer
    length prefix grow by the inserted byte count.
    """
    if not blobs:
        return bytes(raw)
    chain = find_path(raw, map_start, root=root)
    tag = chain[-1]
    if tag.start != map_start or tag.type_name != "MapProperty":
        raise GvasError(f"no MapProperty tag starts at {map_start}")
    num_remove, num_total = struct.unpack_from("<ii", raw, tag.payload)
    _num_remove, entries = parse_map(raw, tag)
    if len(entries) != num_total:
        raise GvasError(f"map {tag.name!r} is not fully parseable; refusing to insert")
    key_type, value_type = tag.type.children
    for blob in blobs:
        try:
            _key, off = read_untagged(blob, 0, key_type)
            _value, off = read_untagged(blob, off, value_type)
        except (GvasError, struct.error) as exc:
            raise GvasError(f"inserted entry does not match {tag.name!r}: {exc}") from exc
        if off != len(blob):
            raise GvasError(f"inserted entry for {tag.name!r} has {len(blob) - off} trailing bytes")
    added = b"".join(blobs)
    buf = bytearray(raw)
    for ancestor in chain:
        (old,) = struct.unpack_from("<i", buf, ancestor.size_offset)
        struct.pack_into("<i", buf, ancestor.size_offset, old + len(added))
        if (ancestor.type_name == "ArrayProperty"
                and ancestor.type.first is not None
                and ancestor.type.first.name == "ByteProperty"):
            (count,) = struct.unpack_from("<i", buf, ancestor.payload)
            struct.pack_into("<i", buf, ancestor.payload, count + len(added))
    struct.pack_into("<ii", buf, tag.payload, num_remove, num_total + len(blobs))
    buf[tag.end:tag.end] = added
    struct.pack_into("<I", buf, 0, len(buf) - 4)
    result = bytes(buf)
    new_tag = find_path(result, map_start, root=root)[-1]
    _num_remove, new_entries = parse_map(result, new_tag)
    if len(new_entries) != num_total + len(blobs):
        raise GvasError(f"map {tag.name!r} did not re-parse after insertion")
    return result


def purge_map_entries(
    raw: bytes,
    should_remove: Callable[["ItemMap", MapEntry, dict], bool],
    *,
    name: str = "Items",
    names: Optional[Sequence[str]] = None,
) -> Tuple[bytes, int, List[Tuple["ItemMap", int]]]:
    """Remove matching entries from every matching map atomically.

    ``should_remove`` receives the owning :class:`ItemMap`, the entry and its
    flattened values. Pass ``names`` to sweep several distinct map names (e.g.
    top-level ``Items`` maps and a weapon's own ``Attachments`` sub-map)
    together. All predicates and byte ranges are resolved against the original
    payload, every surviving ancestor header is updated once, and only then
    are the ranges spliced back-to-front. This is important for nested maps:
    deleting from an ``Attachments`` map first and then reusing the original
    offsets for its enclosing ``Items`` map would cut the wrong bytes.
    ``name`` remains for the common single-map-name case.

    Returns ``(new_raw, total_removed, [(item_map, removed), ...])``.
    """
    targets = list(names) if names is not None else [name]
    maps: List["ItemMap"] = []
    for target_name in targets:
        maps.extend(find_item_maps(raw, name=target_name))
    selected: List[Tuple[ItemMap, MapEntry]] = []
    report: List[Tuple["ItemMap", int]] = []
    for item_map in maps:
        _num_remove, num_total = struct.unpack_from("<ii", raw, item_map.tag.payload)
        if len(item_map.entries) < num_total:
            continue
        values = {id(e): entry_values(raw, e) for e in item_map.entries}
        matches = [
            entry for entry in item_map.entries
            if should_remove(item_map, entry, values[id(entry)])
        ]
        if matches:
            selected.extend((item_map, entry) for entry in matches)
            report.append((item_map, len(matches)))
    if not selected:
        return bytes(raw), 0, []

    # A selected parent entry can contain a selected nested-map entry. Removing
    # the parent already removes the child, so retain only the outermost cuts.
    ordered = sorted(
        {(entry.start, entry.end) for _item_map, entry in selected},
        key=lambda cut: (cut[0], -cut[1]),
    )
    cuts: List[Tuple[int, int]] = []
    for start, end in ordered:
        if not cuts or start >= cuts[-1][1]:
            cuts.append((start, end))
            continue
        if end > cuts[-1][1]:
            raise GvasError(
                f"partially overlapping map-entry ranges "
                f"{cuts[-1][0]}:{cuts[-1][1]} and {start}:{end}"
            )

    cut_starts = [start for start, _end in cuts]
    cut_ends = [end for _start, end in cuts]
    removed_prefix = [0]
    for start, end in cuts:
        removed_prefix.append(removed_prefix[-1] + end - start)

    def inside_cut(start: int, end: int) -> bool:
        index = bisect_right(cut_starts, start) - 1
        return index >= 0 and end <= cut_ends[index]

    def removed_within(tag: PropertyTag) -> int:
        first = bisect_left(cut_starts, tag.payload)
        last = bisect_right(cut_ends, tag.end)
        return removed_prefix[last] - removed_prefix[first] if last > first else 0

    buf = bytearray(raw)

    # Shrink each surviving enclosing property once by all cuts it contains.
    ancestors = {
        ancestor.size_offset: ancestor
        for item_map, _entry in selected
        for ancestor in item_map.chain
    }
    for ancestor in ancestors.values():
        if inside_cut(ancestor.start, ancestor.end):
            continue
        removed_bytes = removed_within(ancestor)
        if not removed_bytes:
            continue
        (old_size,) = struct.unpack_from("<i", buf, ancestor.size_offset)
        struct.pack_into("<i", buf, ancestor.size_offset, old_size - removed_bytes)
        if (
            ancestor.type_name == "ArrayProperty"
            and ancestor.type.first is not None
            and ancestor.type.first.name == "ByteProperty"
        ):
            (old_count,) = struct.unpack_from("<i", buf, ancestor.payload)
            struct.pack_into("<i", buf, ancestor.payload, old_count - removed_bytes)

    # Decrement each surviving map by its own directly-selected entries.
    selected_by_map: dict[int, Tuple[ItemMap, int]] = {}
    for item_map, _entry in selected:
        current = selected_by_map.get(item_map.tag.start)
        selected_by_map[item_map.tag.start] = (
            item_map,
            1 if current is None else current[1] + 1,
        )
    for item_map, removed_count in selected_by_map.values():
        if inside_cut(item_map.tag.start, item_map.tag.end):
            continue
        num_remove, num_total = struct.unpack_from("<ii", buf, item_map.tag.payload)
        struct.pack_into(
            "<ii", buf, item_map.tag.payload, num_remove, num_total - removed_count
        )

    # Copy survivors once instead of shifting the entire tail for every cut.
    struct.pack_into("<I", buf, 0, len(buf) - removed_prefix[-1] - 4)
    view = memoryview(buf)
    pieces = []
    cursor = 0
    for start, end in cuts:
        pieces.append(view[cursor:start])
        cursor = end
    pieces.append(view[cursor:])
    return b"".join(pieces), len(selected), report


    cuts = [(entry.start, entry.end) for entry in doomed]
    removed_bytes = sum(end - start for start, end in cuts)
    kept = len(entries) - len(doomed)

    buf = bytearray(raw)
    # Shrink every enclosing Size header (the map itself is chain[-1]).
    for ancestor in chain:
        (old,) = struct.unpack_from("<i", buf, ancestor.size_offset)
        struct.pack_into("<i", buf, ancestor.size_offset, old - removed_bytes)
        # A raw byte blob also stores its own element count.
        if (ancestor.type_name == "ArrayProperty"
                and ancestor.type.first is not None
                and ancestor.type.first.name == "ByteProperty"):
            (count,) = struct.unpack_from("<i", buf, ancestor.payload)
            struct.pack_into("<i", buf, ancestor.payload, count - removed_bytes)
    struct.pack_into("<ii", buf, tag.payload, num_remove, kept)
    # Splice from the end so earlier offsets stay valid.
    for start, end in sorted(cuts, reverse=True):
        del buf[start:end]
    # The payload opens with a uint32 length prefix covering everything after it.
    struct.pack_into("<I", buf, 0, len(buf) - 4)
    return bytes(buf), len(doomed)
