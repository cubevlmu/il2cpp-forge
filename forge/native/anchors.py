"""Anchor extraction: what a faithful C# body is expected to mention.

Coverage is a proxy, not proof.  A body can name every call and still get a
guard wrong; the score exists only to separate "clearly a stub" from "looks
complete" so expensive review goes where it pays.

Three filters make the score honest, and each one was learned the hard way:

* **Framework calls.**  Native shows ``System_String__op_Equality``; C# writes
  ``==``.  Only calls whose declaring type is a *target* assembly are anchors.
  The mapping comes from the dump index, not from a hardcoded library list.
* **The member itself.**  Evidence always names the method being restored, so
  counting it makes every stub look 20% complete.
* **Generic tails and struct slots.**  ``InitializeAsync_DialogParameter__Int32Enum_``
  ends in type arguments, not a call; ``v.fields.mode`` is a slot of a
  decompiler-synthesised struct (ValueTuple/awaiter), not a field any C# body
  can name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .decompiled import BUSINESS_CALL, FIELD, LITERAL, Reducer

ANCHOR_NAME = re.compile(r"^[A-Z][A-Za-z0-9]{2,}$")
FIELD_WRITE = re.compile(r"fields\.(\w+)\s*=[^=]")
# ``this->fields._count`` is a field of the object under reconstruction;
# ``v178.fields.mode`` is a slot of a decompiler-synthesised struct.  The
# receiver decides which, so it has to be captured.
FIELD_ACCESS = re.compile(
    r"(?:(?P<recv>[A-Za-z_]\w*|\([^)]*\))\s*(?:->|\.)\s*)?fields\.(?P<name>\w+)"
)

# Names that are always structural, never a distinguishing business call.
SELF_LIKE = {"MoveNext", "get_Item", "op_Equality", "op_Inequality", "ctor", "cctor"}

# Slots of decompiler-synthesised structs and BCL value types.  These are
# universal to the toolchain (ValueTuple, List<T>, DateTime, Unity structs),
# not specific to any game, and a C# body reaches them through a property or a
# named member rather than by the slot name.
STRUCT_SLOT = {
    "mode", "source", "token", "klass", "monitor", "result", "runnerPromise",
    "task", "Item1", "Item2", "Item3", "_source", "_token", "cursor", "next",
    "value", "hasValue", "m_task", "m_result", "m_value", "m_SizeDelta",
    "x", "y", "z", "w", "_ticks", "_dateData", "_size", "Item", "Length",
    "_items", "_version", "_syncRoot", "_array", "_count", "_comparer",
    "GameObject", "gameObject", "m_OnValueChanged", "m_Text", "m_Sprite",
    "m_Color", "m_Value", "m_Interactable", "m_FillAmount",
}


@dataclass
class Anchors:
    calls: List[str] = field(default_factory=list)
    fields: List[str] = field(default_factory=list)
    literals: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.calls) + len(self.fields) + len(self.literals)

    def as_dict(self) -> dict:
        return {"calls": self.calls, "fields": self.fields, "literals": self.literals}


@dataclass
class AnchorRules:
    """Per-project anchor policy."""

    reducer: Reducer = field(default_factory=Reducer)
    extra_struct_slots: Iterable[str] = ()

    def __post_init__(self) -> None:
        self._slots = set(STRUCT_SLOT) | {s for s in self.extra_struct_slots if s}

    def is_anchor_call(self, mangled_type: str, name: str, self_name: str) -> bool:
        if self.reducer.is_framework(mangled_type):
            return False
        if not ANCHOR_NAME.match(name) or name in SELF_LIKE or name == self_name:
            return False
        # Generic instantiation tails: ``Foo_Int32Enum_`` / ``Foo_List_1_``.
        if re.search(r"_(?:Int|UInt|String|Object|Boolean|Single|Double|Byte|SByte|"
                     r"Int16|UInt16|Int32|UInt32|Int64|UInt64|Char|Enum|Struct)_$", name):
            return False
        return True

    def is_anchor_field(self, name: str, receiver: str = "") -> bool:
        if name.endswith("_k__BackingField") or name.startswith("__"):
            return False
        # Struct slots only apply to temporaries: ``this`` is the object being
        # reconstructed, and a field named ``mode`` on it is real.
        if receiver and receiver not in ("this",):
            return name not in self._slots
        return True


def business_anchors(lines: Iterable[str], self_name: str, rules: AnchorRules) -> Anchors:
    """``(calls, written fields, literals)`` a faithful body should mention.

    Only fields the native *writes* gate the score.  A field that is merely read
    into a local may be a method receiver (``flick = this.fields._flick`` then
    ``flick.DisableFlick()``) or pure register noise, and nothing in the text
    distinguishes the two.
    """
    anchors = Anchors()
    written: List[str] = []
    for line in lines:
        for mangled, name in BUSINESS_CALL.findall(line):
            if rules.is_anchor_call(mangled, name, self_name):
                if name not in anchors.calls:
                    anchors.calls.append(name)
        for match in FIELD_ACCESS.finditer(line):
            name = match.group("name")
            receiver = match.group("recv") or ""
            if not rules.is_anchor_field(name, receiver):
                continue
            if name not in anchors.fields:
                anchors.fields.append(name)
            if re.search(r"fields\." + re.escape(name) + r"\s*=[^=]", line) and name not in written:
                written.append(name)
        for literal in LITERAL.findall(line):
            if len(literal) > 1 and literal not in anchors.literals:
                anchors.literals.append(literal)
    anchors.fields = written
    return anchors


def coverage(live_text: str, anchors: Anchors) -> tuple[int, int, List[str]]:
    """``(hit, total, missing)`` -- how many anchors the body mentions."""
    total = anchors.total
    if not total:
        return 0, 0, []
    hit = 0
    missing: List[str] = []
    for name in anchors.calls:
        # ``Game_Server__Send`` in native is ``Send`` in the anchor; ``_`` is a
        # word character, so ``\bSend\b`` would not match it.  Allow any
        # non-alphanumeric left boundary instead.
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"\b", live_text):
            hit += 1
        else:
            missing.append(name)
    for name in anchors.fields:
        if re.search(r"\b" + re.escape(name) + r"\b", live_text):
            hit += 1
        else:
            missing.append(name)
    for literal in anchors.literals:
        if literal in live_text:
            hit += 1
        else:
            missing.append(literal)
    return hit, total, missing


def score(live_text: str, anchors: Anchors, min_coverage: float = 0.6) -> dict:
    hit, total, missing = coverage(live_text, anchors)
    ratio = (hit / total) if total else None
    verdict = "no_anchors"
    if total:
        if hit == 0:
            verdict = "silent_closure"
        elif ratio is not None and ratio < min_coverage:
            verdict = "low_coverage"
        else:
            verdict = "covered"
    return {
        "hit": hit,
        "total": total,
        "coverage": None if ratio is None else round(ratio, 3),
        "missing": missing[:12],
        "verdict": verdict,
    }


def self_name_from_mangled(mangled: str) -> str:
    """``Sirius_AD_ADFactory__Create`` -> ``Create``."""
    return mangled.split("__", 1)[1] if "__" in mangled else mangled
