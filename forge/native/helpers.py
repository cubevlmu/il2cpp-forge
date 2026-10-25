"""Identify IL2CPP runtime scaffolding in decompiled output.

IL2CPP compiles managed code into C with a layer of runtime calls around it:
class initialisation, GC write barriers, interface dispatch, null/bounds
throws, array allocation, async builder scheduling.  Those calls are *language
machinery*: they never belong in reconstructed C#, and counting them as
behaviour makes every body look half-missing.

Hardcoding their addresses (``sub_52A76A0``) is exactly what makes a tool
game-specific -- the addresses change with every build.  This module recovers
the mapping per binary, in three ways, strongest first:

``learn_from_symbols``  exported ``il2cpp_*`` names from the ELF symbol table
                        matched to decompiled function addresses.  Exact.
``learn_from_corpus``   statistical shape detection over the decompiled corpus:
                        a helper called right after a ``byte_X & 1`` guard with a
                        ``&Method_...`` first argument is class init; one called
                        on a fresh field store is a write barrier; and so on.
``from_config``         a hand-written ``helpers.json`` (always wins).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Symbol-name substrings -> role.  Matched case-insensitively.
ROLE_BY_SYMBOL: dict[str, str] = {
    "codegen_initialize_method": "class_init",
    "codegen_initialize_runtime_metadata": "class_init",
    "runtime_class_init": "class_init",
    "codegen_class_init": "class_init",
    "class_init": "class_init",
    "gc_wbarrier_set_field": "write_barrier",
    "gc_wbarrier": "write_barrier",
    "write_barrier": "write_barrier",
    "codegen_interface_dispatch": "interface_dispatch",
    "codegen_virtual_dispatch": "interface_dispatch",
    "interface_dispatch": "interface_dispatch",
    "raise_null_reference": "throw_null",
    "null_reference_exception": "throw_null",
    "raise_index_out_of_range": "throw_bounds",
    "index_out_of_range": "throw_bounds",
    "codegen_array_new": "array_new",
    "il2cpp_array_new": "array_new",
    "codegen_string_new": "string_new",
    "il2cpp_string_new": "string_new",
    "object_new": "object_new",
    "codegen_object_new": "object_new",
    "await_uncompleted": "await",
    "awaitunsafeoncompleted": "await",
    "codegen_metadata_use": "metadata_use",
    "codegen_rgctx": "rgctx",
    "codegen_isinst": "cast",
    "codegen_castclass": "cast",
}

# C runtime calls emitted by the decompiler for struct initialisation and
# copies.  ``memset(&v12, 0, 40)`` is declaration zeroing, not behaviour.
C_RUNTIME = {
    "memset", "memcpy", "memmove", "__memcpy_chk", "__memset_chk",
    # decompiler macros, never calls in managed code
    "LOWORD", "HIWORD", "LOBYTE", "HIBYTE", "LODWORD", "HIDWORD",
    "BYTE", "WORD", "DWORD", "QWORD", "BYTEn", "WORDn", "DWORDn", "QWORDn",
}

# Well-known names that can appear verbatim when the binary is not stripped.
KNOWN_NAMES = {
    "il2cpp_codegen_initialize_method",
    "il2cpp_codegen_initialize_runtime_metadata",
    "il2cpp_runtime_class_init",
    "il2cpp_gc_wbarrier_set_field",
    "il2cpp_codegen_write_barrier",
    "il2cpp_codegen_interface_dispatch",
    "il2cpp_codegen_virtual_dispatch",
    "il2cpp_codegen_raise_null_reference_exception",
    "il2cpp_codegen_raise_index_out_of_range_exception",
    "il2cpp_codegen_array_new",
    "il2cpp_array_new",
    "il2cpp_object_new",
    "il2cpp_codegen_object_new",
    "il2cpp_string_new",
    "il2cpp_codegen_string_new_wrapper",
}

ADDRESS_BANNER = re.compile(r"^//-+\s*\(([0-9A-Fa-f]{6,})\)", re.M)
SUB_NAME = re.compile(r"\bsub_([0-9A-Fa-f]{6,})\b")
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
# Two spellings of "has this type been initialised yet":
#   if ( (byte_D5C799E & 1) == 0 )
#   if ( (*(_BYTE *)(klass + 309) & 1) == 0 )
BYTE_GUARD = re.compile(
    r"\(\s*(?:byte_[0-9A-Fa-f]+|\*\(\s*_BYTE\s*\*\s*\)\s*\([^)]*\))\s*&\s*1\s*\)\s*==\s*0"
)
METHOD_ARG = re.compile(r"&(?:Method_|.*?Method_|.*?__Method|.*_TypeInfo)")
FIELD_ARG = re.compile(r"&?(?:this|\w+)->fields\.\w+|&\w+\.fields\.\w+")


@dataclass
class HelperTable:
    """Which native symbols are runtime machinery, and why."""

    roles: Dict[str, List[str]] = field(default_factory=dict)  # role -> symbols
    source: str = "empty"
    confidence: Dict[str, float] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)

    # -- construction -----------------------------------------------------
    @classmethod
    def from_config(cls, payload: dict) -> "HelperTable":
        table = cls(source="config")
        for role, symbols in (payload or {}).items():
            if role in ("_source", "_notes"):
                continue
            if isinstance(symbols, str):
                symbols = [symbols]
            table.roles.setdefault(role, [])
            for symbol in symbols:
                if symbol not in table.roles[role]:
                    table.roles[role].append(symbol)
                table.confidence[symbol] = 1.0
        return table

    @classmethod
    def load(cls, path: Path | str) -> "HelperTable":
        path = Path(path)
        if not path.is_file():
            return cls()
        return cls.from_config(json.loads(path.read_text(encoding="utf-8-sig")))

    # -- queries ----------------------------------------------------------
    def all_symbols(self) -> set[str]:
        out: set[str] = set()
        for symbols in self.roles.values():
            out.update(symbols)
        return out

    def plumbing(self) -> set[str]:
        """Every symbol that should be dropped when reducing a body."""
        return self.all_symbols() | KNOWN_NAMES | C_RUNTIME

    def role_of(self, symbol: str) -> Optional[str]:
        for role, symbols in self.roles.items():
            if symbol in symbols:
                return role
        return None

    def merge(self, other: "HelperTable") -> "HelperTable":
        merged = HelperTable(source=f"{self.source}+{other.source}")
        for table in (self, other):
            for role, symbols in table.roles.items():
                bucket = merged.roles.setdefault(role, [])
                for symbol in symbols:
                    if symbol not in bucket:
                        bucket.append(symbol)
                    merged.confidence[symbol] = max(
                        merged.confidence.get(symbol, 0.0), table.confidence.get(symbol, 0.0)
                    )
        merged.notes.update(other.notes)
        merged.notes.update(self.notes)
        return merged

    def to_json(self) -> dict:
        payload: dict = {role: sorted(symbols) for role, symbols in sorted(self.roles.items())}
        payload["_source"] = self.source
        if self.confidence:
            payload["_confidence"] = {
                k: round(v, 2) for k, v in sorted(self.confidence.items()) if v < 1.0
            }
        return payload


def role_for_symbol(name: str) -> Optional[str]:
    lowered = name.lower()
    for needle, role in ROLE_BY_SYMBOL.items():
        if needle in lowered:
            return role
    return None


def parse_symbol_dump(text: str) -> Dict[int, str]:
    """Parse ``nm -D``-style output into ``{address: name}``.

    Accepts both ``0000000000123abc T il2cpp_codegen_initialize_method`` and
    ``il2cpp_codegen_initialize_method 0x123abc``.
    """
    out: Dict[int, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"[0-9A-Fa-f]{6,16}", parts[0]):
            try:
                out[int(parts[0], 16)] = parts[-1]
            except ValueError:
                continue
        elif len(parts) == 2 and parts[1].lower().startswith("0x"):
            try:
                out[int(parts[1], 16)] = parts[0]
            except ValueError:
                continue
    return out


def learn_from_symbols(
    symbols: Dict[int, str], function_addresses: Dict[int, str]
) -> HelperTable:
    """Match exported symbol addresses to decompiled function names."""
    table = HelperTable(source="symbols")
    for address, name in symbols.items():
        role = role_for_symbol(name)
        if not role:
            continue
        local = function_addresses.get(address)
        if not local:
            continue
        bucket = table.roles.setdefault(role, [])
        if local not in bucket:
            bucket.append(local)
        table.confidence[local] = 1.0
        table.notes[local] = f"symbol {name} @ 0x{address:X}"
    return table


def learn_from_corpus(
    chunks: Iterable[str], min_hits: int = 8, sample: int = 4000
) -> HelperTable:
    """Infer helper roles from call shape across the decompiled corpus.

    Deliberately conservative: a symbol is only classified when one shape
    dominates, and the confidence records how dominant it was.
    """
    shapes: dict[str, Counter] = defaultdict(Counter)
    counts: Counter = Counter()
    for index, chunk in enumerate(chunks):
        if index >= sample:
            break
        lines = chunk.splitlines()
        for position, line in enumerate(lines):
            if not SUB_NAME.search(line):
                continue
            for match in CALL.finditer(line):
                name = match.group(1)
                if not re.fullmatch(r"sub_[0-9A-Fa-f]{6,}", name):
                    continue
                counts[name] += 1
                # Join the call with its continuation lines: the arguments that
                # identify a helper are rarely on the call line itself.
                statement = " ".join(lines[position : position + 8])
                statement = statement.split(";", 1)[0]
                window = "\n".join(lines[max(0, position - 3) : position + 3])
                if BYTE_GUARD.search(window):
                    shapes[name]["class_init"] += 1
                elif "rgctx_data" in window and "method" in statement:
                    shapes[name]["rgctx"] += 1
                elif "Method_" in statement and "&" in statement:
                    shapes[name]["class_init"] += 1
                if FIELD_ARG.search(statement) and statement.count("&") <= 2:
                    shapes[name]["write_barrier"] += 1
                # A write barrier is definitionally the call that follows a
                # field store and receives that same field.  This is far more
                # specific than "takes a field address", which many helpers do.
                previous = lines[position - 1] if position else ""
                store = re.search(r"fields\.(\w+)\s*=[^=]", previous)
                if store and store.group(1) in statement:
                    shapes[name]["write_barrier"] += 1
                if "AwaitUnsafeOnCompleted" in window or (
                    "_d__" in window and "__t__builder" in statement
                ):
                    shapes[name]["await"] += 1
                if "TypeInfo" in line and "=" in line and "->" not in line:
                    shapes[name]["class_init"] += 1

    table = HelperTable(source="corpus")
    for name, counter in shapes.items():
        total = counts[name]
        role, hits = counter.most_common(1)[0]
        if hits < min_hits:
            continue
        dominance = hits / max(total, 1)
        if dominance < 0.5:
            continue
        table.roles.setdefault(role, []).append(name)
        table.confidence[name] = round(dominance, 3)
        table.notes[name] = f"{hits}/{total} calls matched the {role} shape"
    return table


def save(table: HelperTable, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(table.to_json(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return path
