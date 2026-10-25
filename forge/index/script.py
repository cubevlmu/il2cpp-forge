"""Managed <-> native joins from Il2CppDumper's ``script.json``.

Three things live in that file and all three matter:

``ScriptString``      string literals, in metadata order.  ``StringLiteral_N``
                      in decompiled C is exactly ``ScriptString[N]``, so
                      resolving literals turns ``default, default`` guesses into
                      real bodies.
``ScriptMethod``      native address -> mangled native name + native signature.
``ScriptMetadataMethod``  ``Method$Namespace.Type.Method(Args)`` -> function VA.
                      This is the *authoritative* managed-to-native join: it
                      survives overloads, generics and renaming, which string
                      matching cannot.

Addresses in ``ScriptMethod``/``ScriptMetadataMethod`` are virtual addresses in
``libil2cpp.so``; ``dump.cs`` RVAs are image-relative, so ``va = image_base + rva``
and the base is recovered per project (see ``index.builder.detect_image_base``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

METHOD_KEY = re.compile(r"^Method\$(?P<owner>.+)\.(?P<name>[^.]+)\((?P<args>.*)\)$")


@dataclass
class ScriptMethod:
    address: int
    name: str
    signature: str = ""
    type_signature: str = ""


@dataclass
class ScriptMap:
    path: Optional[Path] = None
    strings: List[str] = field(default_factory=list)
    methods: List[ScriptMethod] = field(default_factory=list)
    # (owner, name, param_count) -> function VA
    metadata_methods: Dict[Tuple[str, str, int], int] = field(default_factory=dict)
    # function VA -> "Owner.Method(...)"
    metadata_by_va: Dict[int, str] = field(default_factory=dict)

    def literal(self, index: int) -> Optional[str]:
        if 0 <= index < len(self.strings):
            return self.strings[index]
        return None

    def resolve_literals(self, text: str) -> str:
        """Replace ``StringLiteral_N`` with the quoted literal it refers to."""

        def replace(match: re.Match[str]) -> str:
            value = self.literal(int(match.group(1)))
            if value is None:
                return match.group(0)
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            return f'"{escaped}"'

        return re.sub(r"\bStringLiteral_(\d+)\b", replace, text)

    def method_for(self, owner: str, name: str, params: int) -> Optional[int]:
        return self.metadata_methods.get((owner, name, params))


def _count_args(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    depth = 0
    count = 1
    for char in text:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def load(path: Path | str) -> ScriptMap:
    path = Path(path)
    if not path.is_file():
        return ScriptMap()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    script = ScriptMap(path=path)
    for entry in data.get("ScriptString", []):
        script.strings.append(entry.get("Value", ""))
    for entry in data.get("ScriptMethod", []):
        script.methods.append(
            ScriptMethod(
                address=int(entry.get("Address", 0)),
                name=entry.get("Name", ""),
                signature=entry.get("Signature", ""),
                type_signature=entry.get("TypeSignature", ""),
            )
        )
    for entry in data.get("ScriptMetadataMethod", []):
        name = entry.get("Name", "")
        address = int(entry.get("MethodAddress", 0))
        if not address:
            continue
        script.metadata_by_va.setdefault(address, name)
        match = METHOD_KEY.match(name)
        if not match:
            continue
        owner = match.group("owner").strip()
        method_name = match.group("name").strip()
        params = _count_args(match.group("args"))
        script.metadata_methods.setdefault((owner, method_name, params), address)
    return script


def load_literals(path: Path | str) -> List[str]:
    """``stringliteral.json`` fallback when ``script.json`` is unavailable."""
    path = Path(path)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return [entry.get("value", "") for entry in data]
