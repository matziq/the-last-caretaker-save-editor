"""Extract named map locations from The Last Caretaker's game files (read-only).

The save file stores only the identities and flags of named places such as
Lazarus Complex or Platform Omega; their positions live in the game's world
level. This tool reads the cooked IoStore container without modifying it and
writes ``named_locations.json`` for the editor's map. Re-run it after a game
update:

    python extract_named_locations.py "D:\\Games\\The Last Caretaker"
"""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import zlib
from pathlib import Path

DEFAULT_GAME = Path(r"D:\Games\The.Last.Caretaker.v6.0")
DEFAULT_OUTPUT = Path(__file__).with_name("named_locations.json")
WORLD_PACKAGE = "Voyage/Content/Maps/VoyageWorld2.umap"
STRING_TABLE = "Voyage/Content/LocalizationStringTables/ST_Locations.uasset"
WORLD = "voyageworld2"
NULL_INDEX = 0xFFFFFFFFFFFFFFFF


class ExtractError(Exception):
    pass


def _fstring(data: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    if length == 0:
        return "", offset
    if length < 0:
        end = offset - 2 * length
        return data[offset:end].decode("utf-16-le").rstrip("\0"), end
    if length > 4096 or data[offset + length - 1] != 0:
        raise ExtractError("Invalid string")
    return data[offset:offset + length - 1].decode("latin-1"), offset + length


class IoStore:
    """Minimal reader for an unencrypted, unsigned IoStore container."""

    def __init__(self, toc_path: Path):
        self.toc_path = toc_path
        data = self.toc = toc_path.read_bytes()
        if data[:16] != b"-==--==--==--==-":
            raise ExtractError(f"{toc_path} is not an IoStore table of contents")
        header, count, blocks, block_entry, methods, method_len, block_size, index_size = (
            struct.unpack_from("<8I", data, 20))
        if data[80] & 6 or block_entry != 12:
            raise ExtractError("Encrypted, signed, or unsupported IoStore index")
        self.block_size = block_size
        self.partition_size = struct.unpack_from("<Q", data, 88)[0]
        seeds, overflow = struct.unpack_from("<I", data, 84)[0], struct.unpack_from("<I", data, 96)[0]
        self.offsets_start = header + count * 12
        self.blocks_start = self.offsets_start + count * 10 + (seeds + overflow) * 4
        methods_start = self.blocks_start + blocks * block_entry
        self.methods = ["None"] + [
            data[methods_start + i * method_len:methods_start + (i + 1) * method_len]
            .split(b"\0", 1)[0].decode("ascii") for i in range(methods)]
        index_start = methods_start + methods * method_len
        index = data[index_start:index_start + index_size]
        _mount, off = _fstring(index, 0)
        directory_count = struct.unpack_from("<I", index, off)[0]
        off += 4
        directories = [struct.unpack_from("<4I", index, off + i * 16) for i in range(directory_count)]
        off += directory_count * 16
        file_count = struct.unpack_from("<I", index, off)[0]
        off += 4
        files = [struct.unpack_from("<3I", index, off + i * 12) for i in range(file_count)]
        off += file_count * 12
        string_count = struct.unpack_from("<I", index, off)[0]
        off += 4
        strings = []
        for _ in range(string_count):
            text, off = _fstring(index, off)
            strings.append(text)
        self.files: dict[str, int] = {}
        pending = [(0, "")]
        while pending:
            directory, parent = pending.pop()
            name, child, _sibling, file = directories[directory]
            path = parent + (strings[name] + "/" if name != 0xFFFFFFFF else "")
            while file != 0xFFFFFFFF:
                filename, file, chunk = files[file]
                self.files[(path + strings[filename]).replace("../", "")] = chunk
            while child != 0xFFFFFFFF:
                pending.append((child, path))
                child = directories[child][2]

    def read_path(self, path: str) -> bytes:
        if path not in self.files:
            raise ExtractError(f"Game package not found: {path}")
        return self.read(self.files[path])

    def read(self, chunk: int) -> bytes:
        off = self.offsets_start + chunk * 10
        start = int.from_bytes(self.toc[off:off + 5], "big")
        size = int.from_bytes(self.toc[off + 5:off + 10], "big")
        output = bytearray()
        for block in range(start // self.block_size, (start + size - 1) // self.block_size + 1):
            desc = self.toc[self.blocks_start + block * 12:self.blocks_start + (block + 1) * 12]
            block_offset = int.from_bytes(desc[:5], "little")
            compressed = int.from_bytes(desc[5:8], "little")
            uncompressed = int.from_bytes(desc[8:11], "little")
            method = self.methods[desc[11]]
            partition, relative = divmod(block_offset, self.partition_size)
            stem = self.toc_path.stem + (f"_s{partition}" if partition else "")
            with self.toc_path.with_name(stem + ".ucas").open("rb") as handle:
                handle.seek(relative)
                data = handle.read(compressed)
            if method == "Oodle":
                from ooz import decompress
                data = decompress(data, uncompressed)
            elif method == "Zlib":
                data = zlib.decompress(data)
            elif method != "None":
                raise ExtractError(f"Unsupported compression {method}")
            if len(data) != uncompressed:
                raise ExtractError("Decompressed block has the wrong size")
            output.extend(data)
        begin = start % self.block_size
        return bytes(output[begin:begin + size])


def _package_exports(data: bytes) -> tuple[list[str], list[dict]]:
    has_versioning, header_size = struct.unpack_from("<II", data, 0)
    export_map, bundle_entries = struct.unpack_from("<ii", data, 32)
    if has_versioning:
        raise ExtractError("Versioned zen packages are not supported")
    count, _bytes = struct.unpack_from("<iI", data, 60)
    off = 60 + 16 + 8 * count
    headers = data[off:off + 2 * count]
    off += 2 * count
    names = []
    for i in range(count):
        wide, length = headers[2 * i] & 0x80, ((headers[2 * i] & 0x7F) << 8) | headers[2 * i + 1]
        size = length * (2 if wide else 1)
        names.append(data[off:off + size].decode("utf-16-le" if wide else "latin-1"))
        off += size
    exports = []
    for i in range((bundle_entries - export_map) // 72):
        serial, size, name_index, number, outer = struct.unpack_from("<QQIIQ", data, export_map + 72 * i)
        name = names[name_index & 0x3FFFFFFF] + (f"_{number - 1}" if number else "")
        exports.append({"index": i, "name": name, "outer": outer,
                        "start": header_size + serial, "end": header_size + serial + size})
    if exports and max(e["end"] for e in exports) > len(data):
        raise ExtractError("Package exports exceed package size")
    return names, exports


def _string_table(data: bytes) -> dict[str, str]:
    table: dict[str, str] = {}
    for match in re.finditer(rb"[A-Za-z][A-Za-z0-9_\-]*\x00", data):
        try:
            key, end = _fstring(data, match.start() - 4)
            value, _ = _fstring(data, end)
        except (ExtractError, struct.error, UnicodeDecodeError):
            continue
        if key and value and re.fullmatch(r"[A-Za-z0-9_\-]+", key):
            table.setdefault(key, value)
    return table


def _plausible(vector: tuple[float, float, float]) -> bool:
    x, y, z = vector
    return (all(math.isfinite(value) for value in vector)
            and 100 < abs(x) < 1e8 and 100 < abs(y) < 1e8 and abs(z) < 1e6)


def _friendly(ident: str) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", ident.replace("_", " "))
    return re.sub(r"\s+", " ", text).strip()


def _parse_locator(component: bytes, table: dict[str, str]) -> dict:
    first = re.search(rb"[\x02-\x7f]\x00\x00\x00[\x20-\x7e]+\x00", component)
    if first is None:
        raise ExtractError("Locator name not found")
    names, offset = [], first.start()
    while len(names) < 3 and re.match(rb"[\x02-\x7f]\x00\x00\x00[\x20-\x7e]+\x00",
                                      component[offset:offset + 132]):
        text, offset = _fstring(component, offset)
        names.append(text)
    if len(names) not in (1, 2) or not all(names):
        raise ExtractError("Unexpected locator name fields")
    ident = names[-1]
    offset += 4
    history = component[offset]
    offset += 1
    key = display = ""
    if history == 0x0B:
        offset += 8
        key, offset = _fstring(component, offset)
        display = table.get(key, "")
    elif history == 0x00:
        _namespace, offset = _fstring(component, offset)
        key, offset = _fstring(component, offset)
        display, offset = _fstring(component, offset)
    elif history == 0xFF:
        has_text = struct.unpack_from("<I", component, offset)[0]
        offset += 4
        if has_text:
            display, offset = _fstring(component, offset)
    else:
        raise ExtractError(f"Unsupported display-name text history {history}")
    for start in range(offset, len(component) - 23):
        vector = struct.unpack_from("<3d", component, start)
        if _plausible(vector):
            return {"ident": ident, "key": key, "name": display or _friendly(ident),
                    "x": round(vector[0] / 100, 2), "y": round(vector[1] / 100, 2),
                    "z": round(vector[2] / 100, 2)}
    raise ExtractError(f"No location vector for {ident}")


def extract(game: Path) -> dict:
    toc = game / "Voyage" / "Content" / "Paks" / "pakchunk0-Windows.utoc"
    if not toc.is_file():
        raise ExtractError(f"Game IoStore not found: {toc}")
    store = IoStore(toc)
    table = _string_table(store.read_path(STRING_TABLE))
    data = store.read_path(WORLD_PACKAGE)
    _names, exports = _package_exports(data)
    locations, errors = [], []
    for actor in exports:
        if not actor["name"].startswith("VoyageLocatorActor_UAID_"):
            continue
        components = [e for e in exports if e["outer"] == actor["index"] and e["name"] == "LocatorComponent"]
        try:
            if len(components) != 1:
                raise ExtractError("Expected one LocatorComponent")
            component = data[components[0]["start"]:components[0]["end"]]
            locations.append({"actor": actor["name"], "world": WORLD, **_parse_locator(component, table)})
        except (ExtractError, struct.error, UnicodeDecodeError) as exc:
            errors.append(f"{actor['name']}: {exc}")
    if not locations:
        raise ExtractError("No named locations were found")
    locations.sort(key=lambda item: (item["name"].lower(), item["actor"]))
    return {"source": f"{toc.name}:{WORLD_PACKAGE}", "units": "metres",
            "locations": locations, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("game", nargs="?", type=Path, default=DEFAULT_GAME,
                        help=f"game installation folder (default: {DEFAULT_GAME})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"JSON file to write (default: {DEFAULT_OUTPUT.name} beside this tool)")
    args = parser.parse_args(argv)
    try:
        result = extract(args.game)
    except (ExtractError, OSError, struct.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"Wrote {len(result['locations'])} named locations to {args.output}")
    for error in result["errors"]:
        print(f"Skipped {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
