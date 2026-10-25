"""The open-member queue.

"Nothing left to do" is a claim that has to be measured, not assumed.  Three
shapes hide work:

* an ``UNRESTORED EVIDENCE`` block still in the body -- the honest case;
* a body whose evidence was deleted but whose ``// RECOVERY_PARTIAL`` /
  ``// ASSUMED`` marker survived -- drops out of every evidence-based count and
  looks finished;
* a body that is empty or a bare ``throw new NotImplementedException()`` with no
  marker at all -- indistinguishable from a legitimately empty member unless you
  check the native code, so it is reported as ``empty_silent`` for review.

The queue keeps those apart and joins each entry to its native evidence when the
index has one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..csharp import body as bodylib
from ..csharp import members as csm

KIND_ORDER = (
    "partial",
    "stub",
    "marker_only",
    "empty_silent",
    "legit_empty",
    "restored",
)

BARE_STUB = re.compile(
    r"^\s*(?:throw\s+new\s+(?:NotImplemented|NotSupported)Exception[^;]*;"
    r"|return\s+(?:default(?:\([^)]*\))?|null|0|-1|false|true)\s*;"
    r"|//.*)$"
)


@dataclass
class QueueEntry:
    file: str
    member: str
    params: int
    owner: str = ""
    kind: str = "partial"
    shape: str = "method"
    status: str = ""
    rva: int = 0
    native_name: str = ""
    native_file: str = ""
    line: int = 0
    signature: str = ""
    code_lines: int = 0
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.file}::{self.member}/{self.params}"

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "member": self.member,
            "params": self.params,
            "owner": self.owner,
            "kind": self.kind,
            "shape": self.shape,
            "status": self.status,
            "rva": self.rva,
            "native_name": self.native_name,
            "native_file": self.native_file,
            "line": self.line,
            "signature": self.signature,
            "code_lines": self.code_lines,
            "note": self.note,
        }


@dataclass
class Queue:
    entries: List[QueueEntry] = field(default_factory=list)
    files: int = 0
    members: int = 0
    counts: Dict[str, int] = field(default_factory=dict)

    def open_entries(self) -> List[QueueEntry]:
        return [e for e in self.entries if e.kind in ("partial", "stub", "marker_only", "empty_silent")]

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "members": self.members,
            "counts": self.counts,
            "open": len(self.open_entries()),
            "entries": [e.as_dict() for e in self.entries],
        }


def classify(body: str, name: str, policy: bodylib.BodyPolicy, owner: str = "") -> str:
    live = bodylib.code_of(body, policy)
    has_evidence = policy.has_evidence(body)
    has_marker = any(policy.marker_re.match(line) for line in body.splitlines())
    substantive = [line for line in live if not BARE_STUB.match(line)]
    if has_evidence:
        return "partial" if substantive else "stub"
    if has_marker:
        return "marker_only"
    if not substantive:
        if bodylib.is_legitimately_empty(name, owner):
            return "legit_empty"
        return "empty_silent"
    return "restored"


def _index_by_member(methods: Iterable[dict]) -> Dict[tuple, List[dict]]:
    out: Dict[tuple, List[dict]] = {}
    for row in methods:
        out.setdefault((row.get("name", ""), len(row.get("params", []))), []).append(row)
    return out


def scan(
    src_dir: Path | str,
    methods: Optional[Iterable[dict]] = None,
    policy: bodylib.BodyPolicy = bodylib.DEFAULT_POLICY,
    only: Optional[Iterable[str]] = None,
    suffix: str = ".cs",
) -> Queue:
    """Walk a reconstructed tree and classify every member."""
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        raise SystemExit(f"source tree not found: {src_dir}")
    lookup = _index_by_member(methods or [])
    wanted = set(only) if only else None
    queue = Queue()
    counts: Dict[str, int] = {kind: 0 for kind in KIND_ORDER}

    for path in sorted(src_dir.rglob(f"*{suffix}")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        parsed = csm.parse(text)
        if not parsed.members:
            continue
        queue.files += 1
        stem = path.stem
        for member in parsed.members:
            owner = csm.enclosing_type(text, member.header_start) or stem
            body = csm.body_of(member, text)
            kind = classify(body, member.name, policy, owner)
            counts[kind] = counts.get(kind, 0) + 1
            queue.members += 1
            if kind == "restored" or kind == "legit_empty":
                continue
            if wanted and kind not in wanted:
                continue
            entry = QueueEntry(
                file=str(path.relative_to(src_dir)).replace("\\", "/"),
                member=member.name,
                params=member.params,
                owner=owner,
                kind=kind,
                shape=csm.shape_of(member.name, owner),
                status=bodylib.evidence_meta(body, policy).get("status", ""),
                line=parsed.line_of(member),
                signature=re.sub(r"\s+", " ", member.signature)[:400],
                code_lines=len(bodylib.code_of(body, policy)),
            )
            candidates = lookup.get((member.name, member.params), [])
            if len(candidates) > 1:
                narrowed = [
                    row for row in candidates if row.get("owner", "").split(".")[-1] == stem
                ]
                if narrowed:
                    candidates = narrowed
            # Only link when the owner narrowed it to exactly one function: a
            # wrong pointer turns another type's body into "evidence".
            if len(candidates) == 1:
                row = candidates[0]
                entry.rva = int(row.get("rva") or 0)
                entry.native_name = row.get("native_name", "")
                entry.native_file = row.get("native_file", "")
                if not entry.signature:
                    entry.signature = row.get("signature", "")
            elif len(candidates) > 1:
                entry.note = f"ambiguous:{len(candidates)}"
            queue.entries.append(entry)

    queue.counts = counts
    return queue


def rank(queue: Queue, index_dir: Path | str | None = None) -> List[QueueEntry]:
    """Open entries, hardest evidence first is *not* the order: cheapest wins."""
    order = {"partial": 0, "stub": 1, "marker_only": 2, "empty_silent": 3}
    return sorted(
        queue.open_entries(),
        key=lambda e: (order.get(e.kind, 9), 0 if e.native_name else 1, e.code_lines),
    )


def write_csv(queue: Queue, path: Path | str) -> Path:
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = queue.open_entries()
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].as_dict()) if rows else ["file"])
        writer.writeheader()
        for entry in rows:
            writer.writerow(entry.as_dict())
    return path


def write_json(queue: Queue, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return path
