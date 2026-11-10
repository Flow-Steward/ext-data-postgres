#!/usr/bin/env python3
"""Inspect actual ELF Go build metadata and enforce the native security baseline.

ELF build-info decoding follows Go debug/buildinfo and the catalog inventory.
No native executable is run by this module.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import struct
from pathlib import Path
MAX_METADATA = 1024 * 1024
MAGIC = b"\xff Go buildinf:"
START = bytes.fromhex("3077af0c9274080241e1c107e6d618e6")
END = bytes.fromhex("f932433186182072008242104116d8f2")

def _buildinfo(data):
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ValueError("non-Go native file cannot be inspected")
    order = "<" if data[5] == 1 else ">"
    phoff, shoff = struct.unpack_from(order + "QQ", data, 32)
    phsize, phnum, shsize, shnum, strindex = struct.unpack_from(
        order + "HHHHH", data, 54
    )
    if (
        phnum > 4096
        or shnum > 4096
        or (phnum and phsize != 56)
        or (shnum and shsize != 64)
    ):
        raise ValueError("unsupported ELF header table")
    sections = []
    for index in range(shnum):
        sections.append(
            struct.unpack_from(order + "IIQQQQIIQQ", data, shoff + index * shsize)
        )
    regions = []
    if sections and strindex < len(sections):
        strings = sections[strindex]
        names = data[strings[4] : strings[4] + strings[5]]
        for section in sections:
            if names[section[0] :].split(b"\0", 1)[0] == b".go.buildinfo":
                regions = [(section[4], section[5], section[3])]
                break
    if not regions:
        for index in range(phnum):
            kind, flags, offset, address, _, size, _, _ = struct.unpack_from(
                order + "IIQQQQQQ", data, phoff + index * phsize
            )
            if kind == 1 and flags & 3 == 2:
                regions = [(offset, size, address)]
                break
    if not regions:
        raise ValueError("non-Go native file: no Go build-info section/data segment")
    offset, size, address = regions[0]
    if offset + size > len(data):
        raise ValueError("ELF build-info range exceeds file")
    region = data[offset : offset + size]
    cursor = (-address) % 16
    while cursor + 32 <= len(region) and region[cursor : cursor + 14] != MAGIC:
        cursor += 16
    if cursor + 32 > len(region):
        raise ValueError("non-Go native file: Go build metadata absent")
    if region[cursor + 15] & 2 == 0:
        raise ValueError("unsupported Go pointer build-info format (pre-1.18)")
    cursor += 32

    def string():
        nonlocal cursor
        length = 0
        for index in range(10):
            if cursor >= len(region):
                raise ValueError("truncated Go build-info varint")
            byte = region[cursor]
            cursor += 1
            if index == 9 and byte > 1:
                raise ValueError("overflow Go build-info varint")
            length |= (byte & 127) << (index * 7)
            if byte < 128:
                break
        else:
            raise ValueError("malformed Go build-info varint")
        if length > MAX_METADATA or cursor + length > len(region):
            raise ValueError("Go build-info string size exceeds bounds")
        value = region[cursor : cursor + length]
        cursor += length
        return value

    version, modules = string().decode(), string()
    if not (modules.startswith(START) and modules.endswith(END)):
        raise ValueError("Go module build-info framing missing")
    return version, modules[16:-16].decode()


def _modules(content, gaps, source, replacements=None):
    records, current = [], None
    for line in content.splitlines():
        fields = line.split("\t")
        if fields[0] in {"dep", "mod"}:
            if len(fields) not in {3, 4}:
                raise ValueError("malformed Go module record")
            current = [fields[1], fields[2], False, fields[0]]
            records.append(current)
        elif fields[0] == "=>":
            if current is None or current[2] or len(fields) not in {3, 4}:
                raise ValueError("ambiguous Go module replacement")
            if replacements is not None:
                replacements[current[0]] = {"path": fields[1], "version": fields[2]}
            current[:3] = [fields[1], fields[2], True]
        elif line and fields[0] not in {"path", "build"}:
            raise ValueError("unknown Go build-info record")
    resolved = {}
    for name, version, replaced, kind in records:
        if kind == "mod" and version == "(devel)" and not replaced:
            continue
        if not re.fullmatch(
            r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?", version
        ) or name.startswith((".", "/")):
            gaps.append(
                f"{source}: local/unversioned Go replacement or module {name} cannot be version-checked"
            )
            continue
        if name in resolved:
            raise ValueError("ambiguous duplicate Go module records")
        resolved[name] = version
    return sorted(resolved.items())


def inspect_binary(path: Path) -> dict:
    blob = path.read_bytes()
    toolchain, text = _buildinfo(blob)
    gaps, replacements = [], {}
    modules = dict(_modules(text, gaps, str(path), replacements))
    return {"go_version": toolchain, "modules": modules, "module_gaps": gaps,
            "module_replacements": replacements,
            "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}


def check_floors(record: dict, baseline: dict) -> None:
    def numeric(value: str) -> tuple:
        match = re.fullmatch(r"(?:go|v)?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:[+][0-9A-Za-z.-]+)?", value)
        if not match:
            raise ValueError(f"unsupported version: {value}")
        pre = match.group(4)
        identifiers = tuple((0, int(item)) if item.isdigit() else (1, item)
                            for item in pre.split(".")) if pre else ()
        return (*map(int, match.groups()[:3]), 0 if pre else 1, identifiers)
    if record["go_version"] != baseline["go_version"]:
        raise ValueError(f"toolchain differs from reviewed compiler: {record['go_version']}")
    for name, floor in baseline["modules"].items():
        replacement = record.get("module_replacements", {}).get(name)
        if replacement is not None and replacement["path"] != name:
            raise ValueError(f"unreviewed replacement of security baseline module: {name}")
        version = record["modules"].get(name)
        if version is not None and numeric(version) < numeric(floor):
            raise ValueError(f"module below security floor: {name} {version} < {floor}")
    if record["module_gaps"]:
        raise ValueError("unversioned native dependencies: " + "; ".join(record["module_gaps"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--baseline", type=Path, default=Path(__file__).with_name("native-security-baseline.json"))
    args = parser.parse_args()
    record = inspect_binary(args.binary)
    check_floors(record, json.loads(args.baseline.read_text()))
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
