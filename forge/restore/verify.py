"""Verification: does a written body actually match its evidence?

A body that compiles is not a body that is restored.  This module applies the
tests that the failure modes of real batches demanded:

``EVIDENCE``        evidence block still present -> still queued
``MARKER``          PARTIAL / DRAFT / ASSUMED / INFER_RESTORE survived
``PLACEHOLDER``     ``throw new NotImplementedException("MECHANICAL_TRANSLATION")``
``MECHANICAL``      ``Il2CppObject`` / ``sub_`` / ``->fields`` / ``0LL``
``SCAFFOLDING``     reflection faking interface dispatch
``EMPTY``           no statements, and the name is not legitimately empty
``SILENT_CLOSURE``  native has anchors, body mentions none of them
``LOW_COVERAGE``    anchor coverage below the gate

Coverage is computed against the *reduced* native with async followed into
``MoveNext``, exactly like card generation, so a body that only implements the
trampoline is caught.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..csharp import body as bodylib
from ..csharp import members as csm
from ..native import anchors as anchorlib
from ..native.decompiled import FunctionIndex, MOVENEXT, function_body
from .cards import CardContext


@dataclass
class MemberVerdict:
    file: str
    member: str
    params: int
    issues: List[str] = field(default_factory=list)
    coverage: Optional[float] = None
    hit: int = 0
    total: int = 0
    missing: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "member": self.member,
            "params": self.params,
            "issues": self.issues,
            "coverage": self.coverage,
            "hit": self.hit,
            "total": self.total,
            "missing": self.missing,
        }


def _evidence_for(
    entry: dict,
    context: CardContext,
    snapshot: Dict[tuple, str],
) -> str:
    """Native text for one member: live corpus first, snapshot second."""
    key = (entry.get("file", ""), entry.get("member", ""), int(entry.get("params", 0)))
    if context.function_index:
        name = entry.get("native_name") or ""
        if not name:
            # Recover the name from the snapshot's evidence header if present.
            block = snapshot.get(key, "")
            match = re.search(r"native=(\S+)", block)
            name = match.group(1) if match else ""
        if name:
            raw = context.function_index.read(name)
            if raw:
                body = function_body(raw)
                match = MOVENEXT.search(body)
                if match:
                    target = context.function_index.find_movennext(match.group(1), match.group(2))
                    if target:
                        return function_body(context.function_index.read(target))
                return body
    block = snapshot.get(key, "")
    if block:
        return "\n".join(
            line.split("// C |", 1)[1] for line in block.splitlines() if "// C |" in line
        )
    return ""


def verify(
    entries: Iterable[dict],
    src_dir: Path | str,
    context: CardContext,
    min_coverage: float = 0.6,
    snapshot: Optional[Dict[tuple, str]] = None,
) -> List[MemberVerdict]:
    src_dir = Path(src_dir)
    snapshot = snapshot or {}
    verdicts: List[MemberVerdict] = []
    cache: Dict[str, csm.FileMembers] = {}
    rules = context.rules()
    reducer = context.reducer()

    for entry in entries:
        relative = entry["file"]
        path = src_dir / relative
        verdict = MemberVerdict(
            file=relative, member=entry["member"], params=int(entry.get("params", 0))
        )
        if not path.is_file():
            verdict.issues.append("FILE_MISSING")
            verdicts.append(verdict)
            continue
        if relative not in cache:
            cache[relative] = csm.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        parsed = cache[relative]
        member = parsed.find(verdict.member, verdict.params, entry.get("signature"))
        if member is None:
            verdict.issues.append("MEMBER_MISSING")
            verdicts.append(verdict)
            continue
        raw_body = csm.body_of(member, parsed.text)
        live = "\n".join(bodylib.code_of(raw_body, context.policy))

        evidence_text = _evidence_for(entry, context, snapshot)
        anchors = anchorlib.Anchors()
        if evidence_text:
            # Cards resolve ``StringLiteral_N`` before deriving anchors; doing it
            # here too is what keeps a faithful body from scoring zero on every
            # literal it was told to reproduce.
            reduced = reducer.statements_only(context.script.resolve_literals(evidence_text))
            anchors = anchorlib.business_anchors(reduced, verdict.member, rules)
        score = anchorlib.score(live, anchors, min_coverage)
        verdict.coverage = score["coverage"]
        verdict.hit = score["hit"]
        verdict.total = score["total"]
        verdict.missing = score["missing"]

        owner = csm.enclosing_type(parsed.text, member.header_start)
        result = bodylib.classify(
            raw_body,
            context.policy,
            name=verdict.member,
            owner=owner,
            coverage=score["coverage"],
            min_coverage=min_coverage,
            anchors_total=score["total"],
            anchors_hit=score["hit"],
        )
        verdict.issues = result.issues
        verdicts.append(verdict)
    return verdicts


def load_snapshot(path: Path | str) -> Dict[tuple, str]:
    """Read the evidence snapshot written by ``apply``."""
    path = Path(path)
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    out: Dict[tuple, str] = {}
    for row in rows:
        key = (row.get("file", ""), row.get("member", ""), int(row.get("params", 0)))
        out[key] = row.get("evidence", "")
    return out


def summary(verdicts: List[MemberVerdict]) -> dict:
    failed = [v for v in verdicts if not v.ok]
    by_issue: Dict[str, int] = {}
    for verdict in failed:
        for issue in verdict.issues:
            key = issue.split(":", 1)[0]
            by_issue[key] = by_issue.get(key, 0) + 1
    return {
        "checked": len(verdicts),
        "ok": len(verdicts) - len(failed),
        "failed": len(failed),
        "by_issue": by_issue,
        "failures": [v.as_dict() for v in failed],
    }
