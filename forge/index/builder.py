"""Build the index: methods, types, assemblies, symbols, literals, native join.

The index is the only thing every later stage reads.  It is deliberately plain
JSON/JSONL so an agent can grep it, a human can open it, and no database is
required.

Joining managed metadata to native code is done in order of trust:

1. ``script.json`` ``ScriptMetadataMethod`` -- ``Method$Ns.Type.Name(Args)``
   plus a function VA.  Authoritative: survives overloads and renaming.
2. Mangled name match -- ``Sirius_AD_ADFactory__Create`` against the decompiled
   corpus.  Reliable for ordinary types, weak for generics.
3. Address match -- ``image_base + rva`` against the address banner of a
   decompiled function.  Requires the image base, which is recovered from the
   first two joins by majority vote.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config
from ..dump import dumpscript
from ..native.decompiled import FunctionIndex
from ..util import read_json, write_json, write_jsonl
from . import script as scriptmap

# Namespace roots that are always framework, whatever the assembly list says.
KNOWN_FRAMEWORK_ROOTS = (
    "System", "Microsoft", "Mono", "UnityEngine", "Unity", "TMPro", "Newtonsoft",
    "Cysharp", "UniTask", "UniRx", "Spine", "DG", "Live2D", "CriWare", "Firebase",
    "Google", "AppleAuth", "Adjust", "CodeStage", "Sirenix", "Zenject", "VContainer",
    "MessagePack", "BestHTTP", "DOTween", "MagicOnion", "MessagePipe", "Cinemachine",
)


@dataclass
class IndexSummary:
    dump_cs: str = ""
    types: int = 0
    methods: int = 0
    target_methods: int = 0
    methods_with_native: int = 0
    joined_by_script: int = 0
    joined_by_name: int = 0
    joined_by_address: int = 0
    image_base: int = 0
    native_functions: int = 0
    literals: int = 0
    assemblies: Dict[str, int] = field(default_factory=dict)
    framework_prefixes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dump_cs": self.dump_cs,
            "types": self.types,
            "methods": self.methods,
            "target_methods": self.target_methods,
            "methods_with_native": self.methods_with_native,
            "joined_by_script": self.joined_by_script,
            "joined_by_name": self.joined_by_name,
            "joined_by_address": self.joined_by_address,
            "image_base": self.image_base,
            "image_base_hex": hex(self.image_base),
            "native_functions": self.native_functions,
            "literals": self.literals,
            "assemblies": self.assemblies,
            "framework_prefixes": self.framework_prefixes,
        }


def detect_image_base(
    model: dumpscript.DumpModel,
    script: scriptmap.ScriptMap,
    index: Optional[FunctionIndex] = None,
) -> int:
    """Recover ``va - rva`` by majority vote.

    Uses the authoritative ``script.json`` join first, then mangled-name
    matches.  A wrong base would silently map every method to a stranger's
    native body, so disagreement is reported as zero rather than guessed.
    """
    votes: Counter = Counter()
    by_key: Dict[tuple, int] = {}
    for type_rec in model.types:
        for method in type_rec.methods:
            if method.rva:
                by_key[(method.owner, method.name, method.param_count)] = method.rva
    for key, va in script.metadata_methods.items():
        rva = by_key.get(key)
        if rva:
            votes[va - rva] += 1
    if not votes and index is not None:
        for type_rec in model.types:
            for method in type_rec.methods:
                if not method.rva:
                    continue
                ref = index.get(method.mangled())
                if ref and ref.address:
                    votes[ref.address - method.rva] += 1
    if not votes:
        return 0
    base, hits = votes.most_common(1)[0]
    total = sum(votes.values())
    if hits / total < 0.5:
        return 0
    return base


def _framework_prefixes(model: dumpscript.DumpModel, targets: List[str]) -> List[str]:
    roots = set(KNOWN_FRAMEWORK_ROOTS)
    for type_rec in model.types:
        if type_rec.assembly in targets:
            continue
        if type_rec.namespace:
            roots.add(type_rec.namespace.split(".")[0])
    return sorted(f"{root}_" for root in roots if root)


def build(
    config: Config,
    dump_cs: Path | str | None = None,
    native_dir: Path | str | None = None,
    script_json: Path | str | None = None,
    scope: str = "target",
    index_dir: Path | str | None = None,
    image_base: Optional[int] = None,
) -> IndexSummary:
    dump_path = Path(dump_cs) if dump_cs else config.path_of("dump.dump_cs")
    if not dump_path or not dump_path.is_file():
        raise SystemExit(
            "no dump.cs: pass --dump-cs or set dump.dump_cs in forge.json"
        )
    index_dir = Path(index_dir) if index_dir else config.root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    script_path = Path(script_json) if script_json else config.path_of("dump.script_json")
    script = scriptmap.load(script_path) if script_path else scriptmap.ScriptMap()

    model = dumpscript.parse(dump_path)
    native_root = Path(native_dir) if native_dir else config.path_of("native.c_dir")
    functions: Optional[FunctionIndex] = None
    if native_root and native_root.is_dir():
        functions = FunctionIndex.build(
            native_root, cache_file=index_dir / "native_functions.tsv"
        )

    if image_base is None:
        image_base = detect_image_base(model, script, functions)
    targets = config.target_assemblies
    summary = IndexSummary(
        dump_cs=str(dump_path),
        types=len(model.types),
        methods=model.method_count(),
        image_base=image_base,
        native_functions=len(functions) if functions else 0,
        literals=len(script.strings),
    )

    assemblies: Dict[str, Dict[str, int]] = defaultdict(lambda: {"types": 0, "methods": 0})
    symbols: Dict[str, dict] = {}
    method_rows: List[dict] = []
    type_rows: List[dict] = []
    joined: Counter = Counter()

    for type_rec in model.types:
        target = type_rec.assembly in targets
        assemblies[type_rec.assembly]["types"] += 1
        assemblies[type_rec.assembly]["methods"] += len(type_rec.methods)
        symbols[type_rec.mangled_name] = {
            "type": type_rec.full_name,
            "assembly": type_rec.assembly,
            "target": target,
        }
        if scope == "target" and not target:
            continue
        type_row = type_rec.as_dict()
        type_row["target"] = target
        type_rows.append(type_row)

        for method in type_rec.methods:
            record = method.as_dict()
            record["id"] = method.method_id
            record["mangled"] = method.mangled()
            record["target"] = target
            record["native_name"] = ""
            record["native_file"] = ""
            record["has_native"] = False
            va = 0
            if method.rva:
                va = image_base + method.rva if image_base else 0
                record["va"] = va
            script_va = script.method_for(method.owner, method.name, method.param_count)
            if script_va:
                va = script_va
                record["va"] = va
                joined["script"] += 1
            record["va"] = va
            if functions is not None:
                ref = None
                if va:
                    name = functions.by_address.get(va)
                    if name:
                        ref = functions.get(name)
                        if ref:
                            joined["address"] += 1
                if ref is None:
                    ref = functions.get(method.mangled())
                    if ref:
                        joined["name"] += 1
                if ref is not None:
                    record["native_name"] = ref.name
                    record["native_file"] = _rel(ref.path, native_root)
                    record["has_native"] = True
            if record["has_native"]:
                summary.methods_with_native += 1
            method_rows.append(record)
            if target:
                summary.target_methods += 1

    summary.joined_by_script = joined["script"]
    summary.joined_by_name = joined["name"]
    summary.joined_by_address = joined["address"]
    summary.assemblies = {
        name: {"types": counts["types"], "methods": counts["methods"], "target": name in targets}
        for name, counts in sorted(assemblies.items(), key=lambda item: -item[1]["methods"])
    }
    summary.framework_prefixes = _framework_prefixes(model, targets)

    write_jsonl(index_dir / "methods.jsonl", method_rows)
    write_jsonl(index_dir / "types.jsonl", type_rows)
    write_json(index_dir / "symbols.json", symbols)
    write_json(index_dir / "assemblies.json", summary.assemblies)
    write_json(index_dir / "literals.json", script.strings)
    write_json(index_dir / "summary.json", summary.as_dict())
    write_json(
        index_dir / "join.json",
        {
            "image_base": image_base,
            "image_base_hex": hex(image_base),
            "native_root": str(native_root) if native_root else "",
            "joined_by_script": summary.joined_by_script,
            "joined_by_name": summary.joined_by_name,
            "joined_by_address": summary.joined_by_address,
            "methods_with_native": summary.methods_with_native,
        },
    )
    return summary


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def load_methods(index_dir: Path | str) -> List[dict]:
    path = Path(index_dir) / "methods.jsonl"
    rows: List[dict] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_symbols(index_dir: Path | str) -> Dict[str, dict]:
    return read_json(Path(index_dir) / "symbols.json", {}) or {}


def symbol_roles(symbols: Dict[str, dict]) -> Dict[str, str]:
    """``mangled type -> "framework" | "business"`` for anchor filtering."""
    return {
        name: ("business" if info.get("target") else "framework")
        for name, info in symbols.items()
    }
