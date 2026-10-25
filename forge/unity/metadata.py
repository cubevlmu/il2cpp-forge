"""Read ``global-metadata.dat`` without Il2CppDumper.

Only the header is needed for the toolkit's own purposes -- the IL2CPP metadata
version tells us which dumper/Unity generation we are dealing with, and the
string-literal table lets ``StringLiteral_N`` evidence be resolved to real text
before a model ever sees it.

Layout (little endian, stable since metadata v19)::

    uint32 sanity         0xFAB11BAF
    int32  version
    uint32 stringLiteralOffset, stringLiteralCount      (bytes)
    uint32 stringLiteralDataOffset, stringLiteralDataCount
    uint32 stringOffset, stringCount
    ...

Each string-literal record is ``{uint32 length; uint32 dataIndex}`` and the
text is NUL-terminated UTF-8 inside the literal-data region.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

SANITY = 0xFAB11BAF

# Metadata version -> approximate Unity generation.  ``approx`` because Unity
# has shipped several patch versions on one metadata revision; it is a hint for
# picking a dumper, never an identity claim.
VERSION_HINTS = {
    19: "Unity 5.x",
    21: "Unity 2017.x",
    22: "Unity 2018.1",
    23: "Unity 2018.2",
    24: "Unity 2018.3 - 2019.2",
    25: "Unity 2019.3",
    26: "Unity 2019.4",
    27: "Unity 2020.1 - 2020.2",
    28: "Unity 2021.1",
    29: "Unity 2021.2 - 2021.3",
    30: "Unity 2022.1",
    31: "Unity 2022.2 - 2023.1",
    32: "Unity 2023.2+",
}

UNITY_VERSION_RE = re.compile(rb"(20\d\d\.\d+\.\d+[abfp]\d+)")


@dataclass
class MetadataHeader:
    path: Path
    version: int
    string_literal_offset: int
    string_literal_count: int
    string_literal_data_offset: int
    string_literal_data_count: int
    string_offset: int = 0
    string_count: int = 0

    @property
    def literal_records(self) -> int:
        return self.string_literal_count // 8

    @property
    def unity_hint(self) -> str:
        return VERSION_HINTS.get(self.version, "unknown")

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "version": self.version,
            "unity_hint": self.unity_hint,
            "literal_records": self.literal_records,
            "literal_data_bytes": self.string_literal_data_count,
            "string_records": self.string_count // 8 if self.string_count else 0,
        }


def read_header(path: Path | str) -> MetadataHeader:
    path = Path(path)
    with path.open("rb") as handle:
        blob = handle.read(64)
    if len(blob) < 32:
        raise ValueError(f"{path} is too small to be global-metadata.dat")
    sanity, version = struct.unpack_from("<Ii", blob, 0)
    if sanity != SANITY:
        raise ValueError(f"{path}: bad sanity 0x{sanity:08X} (expected 0x{SANITY:08X})")
    fields = struct.unpack_from("<6I", blob, 8)
    return MetadataHeader(
        path=path,
        version=version,
        string_literal_offset=fields[0],
        string_literal_count=fields[1],
        string_literal_data_offset=fields[2],
        string_literal_data_count=fields[3],
        string_offset=fields[4],
        string_count=fields[5],
    )


def string_literals(path: Path | str, limit: Optional[int] = None) -> List[str]:
    """Decode the string-literal table in metadata order (index == literal id)."""
    header = read_header(path)
    total = header.literal_records
    if limit is not None:
        total = min(total, limit)
    out: List[str] = []
    with Path(path).open("rb") as handle:
        handle.seek(header.string_literal_offset)
        table = handle.read(header.literal_records * 8)
        handle.seek(header.string_literal_data_offset)
        data = handle.read(header.string_literal_data_count)
    for index in range(total):
        length, offset = struct.unpack_from("<II", table, index * 8)
        if offset >= len(data):
            out.append("")
            continue
        raw = data[offset : offset + length]
        out.append(raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace"))
    return out


def detect_unity_version(*candidates: Path | str) -> str:
    """Best-effort Unity version from ``globalgamemanagers`` or the binary."""
    pattern = UNITY_VERSION_RE
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                blob = handle.read(4 << 20)
        except OSError:
            continue
        match = pattern.search(blob)
        if match:
            return match.group(1).decode("ascii")
    return ""
