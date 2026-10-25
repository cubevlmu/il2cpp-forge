"""Parse ``dump.cs`` (Il2CppDumper) into structured metadata.

``dump.cs`` is a flat, one-type-per-block rendering of the IL2CPP metadata:

    // Namespace: Sirius.AD
    public class ADFactory : MonoBehaviour // TypeDefIndex: 1234
    {
        // Fields
        private int _x; // 0x18
        // Properties
        public int X { get; set; }
        // Methods
        // RVA: 0xB12ACEC Offset: 0xB126CEC VA: 0xB12ACEC
        public void Create() { }
    }

The parser is intentionally tolerant: dumper versions differ in attribute
rendering, in whether ``VA:``/``Slot:`` are present, and in how long parameter
lists are wrapped.  Everything the toolkit needs downstream -- type identity,
assembly, RVA, signature, parameter types -- is preserved; the rest is kept as
raw text so a human (or an agent) can still read it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

IMAGE_RE = re.compile(r"^//\s*Image\s+(\d+):\s*(.+?)\s*-\s*(\d+)\s*$")
NAMESPACE_RE = re.compile(r"^//\s*Namespace:\s*(.*)$")
TYPE_HEAD_RE = re.compile(
    r"^(?P<mods>(?:(?:public|private|protected|internal|sealed|abstract|static|partial|unsafe|new|readonly|ref)\s+)*)"
    r"(?P<kind>class|struct|interface|enum|record)\s+"
)
RVA_ANY_RE = re.compile(r"^//\s*RVA:\s*(-?\d+|0x[0-9A-Fa-f]+)")
RVA_DETAIL_RE = re.compile(
    r"RVA:\s*(0x[0-9A-Fa-f]+|-?\d+)\s*(?:Offset:\s*(0x[0-9A-Fa-f]+|-?\d+))?"
    r"(?:\s*VA:\s*(0x[0-9A-Fa-f]+|-?\d+))?(?:\s*Slot:\s*(\d+))?"
)
GENERIC_INST_HEAD = re.compile(r"/\*\s*GenericInstMethod", re.I)
SECTION_RE = re.compile(r"^//\s*(Fields|Properties|Methods|Events|Nested types)\s*$")
FIELD_RE = re.compile(
    r"^(?P<decl>(?:(?:public|private|protected|internal|static|readonly|volatile|const|new|unsafe|extern)\s+)*)"
    r"(?P<type>[\w\.<>,\[\]\?]+(?:\s*\*\s*)?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?P<value>[^;]+))?;"
    r"(?:\s*//\s*(?P<offset>0x[0-9A-Fa-f]+))?"
)
PROPERTY_RE = re.compile(
    r"^(?P<decl>(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|new|readonly|unsafe)\s+)*)"
    r"(?P<type>[\w\.<>,\[\]\?\s]+?)\s+(?P<name>[A-Za-z_]\w*)\s*\{"
    r"(?P<accessors>[^}]*)\}"
)
ATTRIBUTE_RE = re.compile(r"^\s*\[(?P<body>.+)\]\s*$")
MODIFIERS = {
    "public", "private", "protected", "internal", "static", "readonly", "volatile",
    "const", "new", "unsafe", "extern", "abstract", "virtual", "override", "sealed",
    "partial", "async", "ref", "out", "in", "params", "this", "required",
}


@dataclass
class Param:
    type: str
    name: str = ""
    modifiers: str = ""

    def as_dict(self) -> dict:
        return {"type": self.type, "name": self.name, "modifiers": self.modifiers}


@dataclass
class MethodRec:
    name: str
    owner: str  # full type name, namespace included
    namespace: str
    assembly: str
    return_type: str = ""
    params: List[Param] = field(default_factory=list)
    is_static: bool = False
    is_abstract: bool = False
    is_virtual: bool = False
    is_ctor: bool = False
    rva: int = 0
    offset: int = 0
    va: int = 0
    slot: Optional[int] = None
    attributes: List[str] = field(default_factory=list)
    signature: str = ""
    line: int = 0
    generic_instances: List[dict] = field(default_factory=list)

    @property
    def param_count(self) -> int:
        return len(self.params)

    @property
    def param_types(self) -> List[str]:
        return [p.type for p in self.params]

    @property
    def method_id(self) -> str:
        return f"{self.owner}::{self.name}/{self.param_count}"

    def mangled_type(self) -> str:
        return mangle(self.owner)

    def mangled(self) -> str:
        return f"{mangle(self.owner)}__{mangle_member(self)}"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "owner": self.owner,
            "namespace": self.namespace,
            "assembly": self.assembly,
            "return_type": self.return_type,
            "params": [p.as_dict() for p in self.params],
            "is_static": self.is_static,
            "is_abstract": self.is_abstract,
            "is_virtual": self.is_virtual,
            "is_ctor": self.is_ctor,
            "rva": self.rva,
            "offset": self.offset,
            "va": self.va,
            "slot": self.slot,
            "attributes": self.attributes,
            "signature": self.signature,
            "line": self.line,
        }


@dataclass
class FieldRec:
    name: str
    type: str
    owner: str
    offset: Optional[int] = None
    is_static: bool = False
    is_const: bool = False
    value: str = ""
    attributes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "owner": self.owner,
            "offset": self.offset,
            "is_static": self.is_static,
            "is_const": self.is_const,
            "value": self.value,
            "attributes": self.attributes,
        }


@dataclass
class PropertyRec:
    name: str
    type: str
    owner: str
    accessors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "owner": self.owner,
            "accessors": self.accessors,
        }


@dataclass
class TypeRec:
    namespace: str
    name: str
    kind: str
    assembly: str
    typedef_index: Optional[int] = None
    modifiers: str = ""
    base_types: List[str] = field(default_factory=list)
    attributes: List[str] = field(default_factory=list)
    fields: List[FieldRec] = field(default_factory=list)
    properties: List[PropertyRec] = field(default_factory=list)
    methods: List[MethodRec] = field(default_factory=list)
    line: int = 0

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @property
    def simple_name(self) -> str:
        return self.name.split(".")[-1]

    @property
    def mangled_name(self) -> str:
        return mangle(self.full_name)

    def as_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "full_name": self.full_name,
            "kind": self.kind,
            "assembly": self.assembly,
            "typedef_index": self.typedef_index,
            "modifiers": self.modifiers,
            "base_types": self.base_types,
            "attributes": self.attributes,
            "fields": [f.as_dict() for f in self.fields],
            "properties": [p.as_dict() for p in self.properties],
            "line": self.line,
        }


@dataclass
class DumpModel:
    path: Path
    images: Dict[str, int] = field(default_factory=dict)  # assembly -> first typedef index
    types: List[TypeRec] = field(default_factory=list)

    @property
    def methods(self) -> Iterable[MethodRec]:
        for type_rec in self.types:
            for method in type_rec.methods:
                yield method

    def method_count(self) -> int:
        return sum(len(t.methods) for t in self.types)

    def type_by_full_name(self) -> Dict[str, TypeRec]:
        return {t.full_name: t for t in self.types}


def mangle(name: str) -> str:
    """Mangled native type name: ``Sirius.AD.Foo<Bar>`` -> ``Sirius_AD_Foo_Bar_``.

    The exact spelling differs between dumpers, so this is used for *matching
    hints*, never as an identity.
    """
    out = name.replace("::", "_").replace(".", "_").replace("+", "_")
    out = out.replace("<", "_").replace(">", "_").replace(",", "_").replace(" ", "")
    out = out.replace("[]", "_array").replace("[", "_").replace("]", "_")
    return re.sub(r"_+", "_", out)


def mangle_member(method: MethodRec) -> str:
    if method.is_ctor:
        return "_ctor"
    name = method.name
    if "<" in name:
        name = re.sub(r"[<>]", "_", name)
    return re.sub(r"_+", "_", name)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _strip_attributes(line: str) -> str:
    return re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", line)


def _split_params(text: str) -> List[Param]:
    params: List[Param] = []
    depth = 0
    current: List[str] = []
    for char in text:
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        if char == "," and depth == 0:
            params.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        params.append("".join(current))
    out: List[Param] = []
    for raw in params:
        cleaned = re.sub(r"\[[^\]]*\]", "", raw).strip()
        if not cleaned:
            continue
        tokens = cleaned.split()
        modifiers = ""
        while tokens and tokens[0] in ("ref", "out", "in", "params", "this"):
            modifiers = (modifiers + " " + tokens.pop(0)).strip()
        if len(tokens) >= 2:
            out.append(Param(type=" ".join(tokens[:-1]), name=tokens[-1], modifiers=modifiers))
        elif tokens:
            out.append(Param(type=tokens[0], modifiers=modifiers))
    return out


def _parse_method_decl(
    decl: str, owner: TypeRec, namespace: str, assembly: str, line: int
) -> Optional[MethodRec]:
    decl = _strip_attributes(decl).strip()
    if not decl or "(" not in decl:
        return None
    head, _, tail = decl.partition("(")
    # Drop the body marker that follows the parameter list.
    close = tail.rfind(")")
    inner = tail[:close] if close != -1 else tail
    head_tokens = head.split()
    if not head_tokens:
        return None
    name = head_tokens[-1]
    name = re.sub(r"<[^<>]*>$", "", name)
    if name in MODIFIERS:
        return None
    return_type = " ".join(token for token in head_tokens[:-1] if token not in MODIFIERS)
    modifiers = {t for t in head_tokens[:-1] if t in MODIFIERS}
    is_ctor = name == owner.simple_name or name == owner.name
    if is_ctor:
        name = ".ctor" if "static" not in modifiers else ".cctor"
    return MethodRec(
        name=name,
        owner=owner.full_name,
        namespace=namespace,
        assembly=assembly,
        return_type=return_type if not is_ctor else "",
        params=_split_params(inner),
        is_static="static" in modifiers,
        is_abstract="abstract" in modifiers,
        is_virtual=bool({"virtual", "override", "abstract"} & modifiers),
        is_ctor=is_ctor,
        signature=re.sub(r"\s+", " ", decl).strip(),
        line=line,
    )


def _parse_type_header(
    line: str,
) -> Optional[tuple[str, str, str, List[str], Optional[int]]]:
    """``(modifiers, kind, name, base_types, typedef_index)``.

    Generic type names contain spaces (``<>f__AnonymousType5<<a>j__TPar, <b>j__TPar>``),
    so the name is taken up to the first space at angle-bracket depth zero.
    """
    stripped = line.strip()
    match = TYPE_HEAD_RE.match(stripped)
    if not match:
        return None
    rest = stripped[match.end() :]
    depth = 0
    end = len(rest)
    for position, char in enumerate(rest):
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif char == " " and depth == 0:
            end = position
            break
    name = rest[:end].strip()
    tail = rest[end:]
    tail_code = tail.split("//", 1)[0]
    index_match = re.search(r"TypeDefIndex:\s*(\d+)", tail)
    bases: List[str] = []
    if ":" in tail_code:
        base_text = tail_code.split(":", 1)[1]
        bases = [b.strip() for b in base_text.split(",") if b.strip()]
    return (
        match.group("mods").strip(),
        match.group("kind"),
        name,
        bases,
        int(index_match.group(1)) if index_match else None,
    )


def _next_nonempty(lines: List[str], start: int) -> str:
    for position in range(start, min(start + 4, len(lines))):
        if lines[position].strip():
            return lines[position].strip()
    return ""


def _int_of(value: Optional[str]) -> int:
    if not value:
        return 0
    try:
        parsed = int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        return 0
    return max(parsed, 0)


def parse(path: Path | str) -> DumpModel:
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    model = DumpModel(path=path)

    # Il2CppDumper writes the image table once, at the top; each image owns a
    # contiguous range of TypeDefIndex values, which is how a type is assigned
    # to its assembly.
    ranges: List[tuple[int, str]] = []
    for line in lines:
        image = IMAGE_RE.match(line.strip())
        if image:
            ranges.append((int(image.group(3)), image.group(2)))
            model.images.setdefault(image.group(2), int(image.group(3)))
    ranges.sort()

    def assembly_for(index: Optional[int]) -> str:
        if index is None:
            return ""
        name = ""
        for start, assembly_name in ranges:
            if index >= start:
                name = assembly_name
            else:
                break
        return name

    namespace = ""
    current: Optional[TypeRec] = None
    section = ""
    pending_rva = pending_offset = pending_va = 0
    pending_slot: Optional[int] = None
    pending_attrs: List[str] = []
    pending_type_attrs: List[str] = []
    decl_buffer: List[str] = []
    decl_start = 0
    in_block_comment = False
    in_generic_inst = False
    generic_rows: List[dict] = []

    def flush_decl() -> None:
        nonlocal decl_buffer, pending_rva, pending_offset, pending_va, pending_slot
        nonlocal pending_attrs, generic_rows
        if current is None or not decl_buffer:
            decl_buffer = []
            generic_rows = []
            return
        decl = " ".join(part.strip() for part in decl_buffer).strip()
        method = _parse_method_decl(decl, current, namespace, current.assembly, decl_start)
        if method is not None:
            method.rva = pending_rva
            method.offset = pending_offset
            method.va = pending_va
            method.slot = pending_slot
            method.attributes = pending_attrs[:]
            method.generic_instances = generic_rows[:]
            current.methods.append(method)
        decl_buffer = []
        generic_rows = []
        pending_rva = pending_offset = pending_va = 0
        pending_slot = None
        pending_attrs = []

    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue

        # -- block comments (generic instantiations live in one) -----------
        if in_generic_inst:
            if "*/" in stripped:
                in_generic_inst = False
                if current is not None and current.methods:
                    current.methods[-1].generic_instances = generic_rows[:]
                generic_rows = []
                continue
            rva = RVA_DETAIL_RE.search(stripped)
            if rva:
                generic_rows.append(
                    {
                        "rva": _int_of(rva.group(1)),
                        "offset": _int_of(rva.group(2)),
                        "va": _int_of(rva.group(3)),
                    }
                )
                continue
            if stripped.startswith("|-") or stripped.startswith("|"):
                text = stripped.lstrip("|-").strip()
                if text and generic_rows:
                    generic_rows[-1]["signature"] = text
            continue
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if GENERIC_INST_HEAD.search(stripped):
                in_generic_inst = True
            elif "*/" not in stripped:
                in_block_comment = True
            continue

        image = IMAGE_RE.match(stripped)
        if image:
            continue
        namespace_match = NAMESPACE_RE.match(stripped)
        if namespace_match:
            namespace = namespace_match.group(1).strip()
            continue

        # -- type header ---------------------------------------------------
        if current is None:
            if stripped.startswith("["):
                pending_type_attrs.append(stripped)
                continue
            header = _parse_type_header(stripped)
            if header and (header[4] is not None or _next_nonempty(lines, number) == "{"):
                mods, kind, name, bases, index = header
                current = TypeRec(
                    namespace=namespace,
                    name=name,
                    kind=kind,
                    assembly=assembly_for(index),
                    typedef_index=index,
                    modifiers=mods,
                    base_types=bases,
                    attributes=pending_type_attrs[:],
                    line=number,
                )
                model.types.append(current)
                pending_type_attrs = []
                section = ""
            continue

        # -- inside a type -------------------------------------------------
        if stripped == "}":
            flush_decl()
            current = None
            section = ""
            pending_attrs = []
            continue

        section_match = SECTION_RE.match(stripped)
        if section_match:
            flush_decl()
            section = section_match.group(1)
            continue

        if RVA_ANY_RE.match(stripped):
            flush_decl()
            detail = RVA_DETAIL_RE.search(stripped)
            pending_rva = _int_of(detail.group(1)) if detail else 0
            pending_offset = _int_of(detail.group(2)) if detail else 0
            pending_va = _int_of(detail.group(3)) if detail else 0
            pending_slot = int(detail.group(4)) if detail and detail.group(4) else None
            continue

        if stripped.startswith("//"):
            continue

        attribute = ATTRIBUTE_RE.match(stripped)
        if attribute:
            pending_attrs.append(attribute.group("body"))
            continue

        if decl_buffer:
            decl_buffer.append(stripped)
            if "{" in stripped:
                flush_decl()
            continue

        if section == "Properties":
            prop = PROPERTY_RE.match(stripped)
            if prop:
                accessors = re.findall(r"\b(get|set|init|add|remove)\b", prop.group("accessors"))
                current.properties.append(
                    PropertyRec(
                        name=prop.group("name"),
                        type=prop.group("type").strip(),
                        owner=current.full_name,
                        accessors=accessors,
                    )
                )
                pending_attrs = []
                continue

        if section in ("Fields", "") and ";" in stripped and "(" not in stripped:
            field_match = FIELD_RE.match(stripped)
            if field_match:
                decl = field_match.group("decl")
                current.fields.append(
                    FieldRec(
                        name=field_match.group("name"),
                        type=field_match.group("type").strip(),
                        owner=current.full_name,
                        offset=_int_of(field_match.group("offset")),
                        is_static="static" in decl,
                        is_const="const" in decl,
                        value=(field_match.group("value") or "").strip(),
                        attributes=pending_attrs[:],
                    )
                )
                pending_attrs = []
                continue

        if section == "Methods" or "(" in stripped:
            decl_buffer = [stripped]
            decl_start = number
            if "{" in stripped:
                flush_decl()
            continue

        pending_attrs = []

    return model


def summary(model: DumpModel) -> dict:
    kinds: Dict[str, int] = {}
    for type_rec in model.types:
        kinds[type_rec.kind] = kinds.get(type_rec.kind, 0) + 1
    methods = list(model.methods)
    return {
        "types": len(model.types),
        "methods": len(methods),
        "methods_with_rva": sum(1 for m in methods if m.rva),
        "images": len(model.images),
        "kinds": kinds,
    }
