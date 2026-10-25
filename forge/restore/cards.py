"""Method cards: everything one member needs, and nothing else.

A card is the unit of work an agent (or a human) receives.  It is deliberately
self-contained and bounded:

* the managed signature and the owner's field/property layout, so the body can
  name things that actually exist;
* the *reduced* native body -- behaviour statements only -- plus the raw
  decompilation when it is short enough to be useful;
* resolved string literals, because ``StringLiteral_8200`` is unreadable and a
  model that cannot read a literal invents one;
* the anchors a faithful body is expected to mention, so the result can be
  scored instead of trusted;
* the state-machine body for async methods, since the trampoline contains no
  business logic at all.

Cards are JSON on disk and Markdown on screen; both come from the same record.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..csharp import body as bodylib
from ..csharp import members as csm
from ..index import script as scriptmap
from ..native import anchors as anchorlib
from ..native.decompiled import FunctionIndex, MOVENEXT, Reducer, function_body, signature_of
from ..native.helpers import HelperTable
from .queue import QueueEntry

MAX_RAW_LINES = 260
PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def prompt_text(name: str) -> str:
    """Read one of the packaged contracts, or ``""`` when it is not installed.

    A card that arrives without its contract invites the model to invent one, so
    the file is loaded by default rather than passed in by every caller.
    """
    path = PROMPT_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclass
class CardContext:
    """Shared, expensive-to-build state for card generation."""

    function_index: Optional[FunctionIndex] = None
    script: scriptmap.ScriptMap = field(default_factory=scriptmap.ScriptMap)
    helpers: HelperTable = field(default_factory=HelperTable)
    symbol_roles: Dict[str, str] = field(default_factory=dict)
    framework_prefixes: List[str] = field(default_factory=list)
    types: Dict[str, dict] = field(default_factory=dict)
    policy: bodylib.BodyPolicy = field(default_factory=bodylib.BodyPolicy)
    native_root: Optional[Path] = None

    def __post_init__(self) -> None:
        self.symbol_roles = self.symbol_roles or {}
        self.framework_prefixes = self.framework_prefixes or []
        self.types = self.types or {}

    def reducer(self) -> Reducer:
        return Reducer(
            plumbing=self.helpers.plumbing(),
            framework_prefixes=self.framework_prefixes,
            symbol_roles=self.symbol_roles,
        )

    def rules(self) -> anchorlib.AnchorRules:
        return anchorlib.AnchorRules(reducer=self.reducer())


def _type_context(owner: str, types: Dict[str, dict], hint: str = "") -> dict:
    """The owner's field/property layout, keyed by the index's full name.

    The queue reports the bare class name while the index is keyed by
    ``Namespace.Type``; a plain ``types.get(owner)`` therefore returns nothing and
    the card silently ships without the layout the contract tells the model to
    name members from.
    """
    record = types.get(owner)
    if record is None and owner:
        candidates = [
            row
            for row in types.values()
            if row.get("full_name", "").split(".")[-1] == owner
        ]
        if len(candidates) > 1 and hint:
            # Namespace segments usually mirror the *folder* path.  Compare whole
            # directory segments: matching against the file stem lets ``AD`` score
            # a hit inside ``ADFactory.cs``.
            segments = {
                part.lower()
                for part in re.split(r"[\\/]+", hint)
                if part and part not in (".", "..")
            }
            segments.discard(hint.replace("\\", "/").split("/")[-1].lower())
            scored = [
                (
                    sum(
                        1
                        for part in row.get("full_name", "").split(".")[:-1]
                        if part.lower() in segments
                    ),
                    row,
                )
                for row in candidates
            ]
            scored.sort(key=lambda item: -item[0])
            if scored[0][0] > 0:
                candidates = [scored[0][1]]
        record = candidates[0] if candidates else None
    if not record:
        return {}
    return {
        "full_name": record.get("full_name", owner),
        "kind": record.get("kind", ""),
        "base_types": record.get("base_types", []),
        "fields": [
            {"name": f.get("name"), "type": f.get("type"), "offset": f.get("offset"),
             "static": f.get("is_static", False)}
            for f in record.get("fields", [])
        ],
        "properties": [
            {"name": p.get("name"), "type": p.get("type"), "accessors": p.get("accessors", [])}
            for p in record.get("properties", [])
        ],
    }


def _native_evidence(
    entry: QueueEntry,
    context: CardContext,
) -> dict:
    """Raw + reduced native body, following async into ``MoveNext``."""
    evidence: dict = {
        "native_name": entry.native_name,
        "native_file": entry.native_file,
        "raw": "",
        "reduced": [],
        "signature": "",
        "movenext": "",
        "truncated": False,
    }
    if not context.function_index or not entry.native_name:
        return evidence
    raw = context.function_index.read(entry.native_name)
    if not raw:
        return evidence
    evidence["signature"] = signature_of(raw)
    body = function_body(raw)
    evidence["raw"] = _truncate(_raw_view(context.script.resolve_literals(body)))
    evidence["truncated"] = evidence["raw"].endswith("// ... truncated")
    reducer = context.reducer()
    evidence["reduced"] = [context.script.resolve_literals(line) for line in reducer.reduce(body)]

    # An async method's own native body is a builder trampoline: it contains no
    # business logic.  Show the state machine instead, and keep the wrapper
    # under its own key for reference.
    match = MOVENEXT.search(body)
    if match:
        target = context.function_index.find_movennext(match.group(1), match.group(2))
        if target:
            state_raw = context.function_index.read(target)
            state_body = function_body(state_raw)
            evidence["movenext"] = target
            evidence["wrapper"] = evidence["raw"]
            evidence["raw"] = _truncate(_raw_view(context.script.resolve_literals(state_body)))
            evidence["reduced"] = [
                context.script.resolve_literals(line) for line in reducer.reduce(state_body)
            ]
    return evidence


def _raw_view(text: str) -> str:
    """Skip the leading local-declaration block of a decompiled body.

    Hex-Rays emits one annotated declaration per register (``int v12; // w4``)
    before any statement; on a large method that is 200 lines of noise in front
    of the code the reader actually wants.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if "; //" in stripped and "(" not in stripped:
            index += 1
            continue
        break
    return "\n".join(lines[index:])


def _truncate(text: str, limit: int = MAX_RAW_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + "\n// ... truncated"


def _anchors_of(evidence: dict, entry: QueueEntry, context: CardContext) -> dict:
    rules = context.rules()
    lines: List[str] = []
    if evidence.get("reduced"):
        lines = evidence["reduced"]
    elif evidence.get("raw"):
        lines = context.reducer().statements_only(evidence["raw"])
    if not lines:
        return {"calls": [], "fields": [], "literals": [], "total": 0}
    found = anchorlib.business_anchors(lines, entry.member, rules)
    payload = found.as_dict()
    payload["total"] = found.total
    return payload


def build_card(
    entry: QueueEntry,
    context: CardContext,
    src_dir: Optional[Path] = None,
    instructions: str = "",
) -> dict:
    card: dict = {
        "id": f"{entry.owner}::{entry.member}/{entry.params}" if entry.owner else entry.key,
        "queue_key": entry.key,
        "file": entry.file,
        "member": entry.member,
        "params": entry.params,
        "line": entry.line,
        "kind": entry.kind,
        "shape": entry.shape,
        "signature": entry.signature,
        "managed": {
            "owner": entry.owner,
            "rva": entry.rva,
            "rva_hex": hex(entry.rva) if entry.rva else "",
            "status": entry.status,
        },
        "type_context": _type_context(entry.owner, context.types, hint=entry.file),
        "current_body": "",
        "evidence": {},
        "anchors": {},
        "instructions": instructions or prompt_text("restore_member.md"),
        "output_contract": {
            "type": "json",
            "fields": ["member", "params", "body", "confidence", "notes"],
            "body": "inner statements only, no outer braces",
            "confidence": "high | medium | low",
            "notes": "one line per inferred point, each prefixed INFER:",
        },
    }

    if src_dir is not None:
        path = Path(src_dir) / entry.file
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            parsed = csm.parse(text)
            member = parsed.find(entry.member, entry.params, entry.signature)
            if member is not None:
                raw_body = csm.body_of(member, text)
                card["current_body"] = bodylib.inner_of(raw_body, context.policy).strip()
                meta = bodylib.evidence_meta(raw_body, context.policy)
                if meta:
                    card["managed"].update(meta)

    evidence = _native_evidence(entry, context)
    card["evidence"] = evidence
    card["anchors"] = _anchors_of(evidence, entry, context)
    return card


def render_markdown(card: dict) -> str:
    managed = card.get("managed", {})
    evidence = card.get("evidence", {})
    anchors = card.get("anchors", {})
    lines: List[str] = []
    lines.append(f"# {card['id']}")
    lines.append("")
    lines.append(f"- file: `{card['file']}` (line {card.get('line')})")
    lines.append(f"- kind: {card.get('kind')} / {card.get('shape')}")
    lines.append(f"- rva: `{managed.get('rva_hex') or '-'}`  native: `{evidence.get('native_name') or '-'}`")
    lines.append("")
    lines.append("## Managed signature")
    lines.append("```csharp")
    lines.append(card.get("signature") or "(missing)")
    lines.append("```")
    context = card.get("type_context") or {}
    if context.get("fields"):
        lines.append("## Fields")
        lines.append("```csharp")
        for field in context["fields"]:
            lines.append(f"{field['type']} {field['name']}; // 0x{field['offset'] or 0:X}")
        lines.append("```")
    if card.get("current_body"):
        lines.append("## Current body")
        lines.append("```csharp")
        lines.append(card["current_body"])
        lines.append("```")
    if evidence.get("reduced"):
        lines.append("## Native behaviour (reduced)")
        lines.append("```c")
        lines.extend(evidence["reduced"])
        lines.append("```")
    if evidence.get("raw"):
        lines.append("## Native raw")
        lines.append("```c")
        lines.append(evidence["raw"])
        lines.append("```")
    if anchors.get("calls") or anchors.get("fields") or anchors.get("literals"):
        lines.append("## Anchors a faithful body should mention")
        lines.append(
            f"- calls: {', '.join(anchors.get('calls', [])) or '-'}\n"
            f"- written fields: {', '.join(anchors.get('fields', [])) or '-'}\n"
            f"- literals: {', '.join(repr(x) for x in anchors.get('literals', [])) or '-'}"
        )
    if card.get("instructions"):
        lines.append("## Instructions")
        lines.append(card["instructions"])
    return "\n".join(lines) + "\n"


def write_card(card: dict, directory: Path | str, markdown: bool = False) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\.\-]+", "_", card["id"])[:120]
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    if markdown:
        (directory / f"{stem}.md").write_text(render_markdown(card), encoding="utf-8")
    return path
