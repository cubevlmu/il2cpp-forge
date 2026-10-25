"""Write restored bodies into the tree from a JSON patch.

Patch format (a list, one entry per member)::

    [
      {"file": "Foo/Bar.cs", "member": "CreateAsync", "params": 2,
       "body": "var x = ...;\nreturn x;",
       "note": "why this body is faithful"}
    ]

``body`` is the *inner* text only -- no outer braces.  The writer adds the
braces and re-indents to the member's own indentation, so a patch may be written
at any indentation and still land correctly.

Guards, all of which exist because they were violated at least once:

* the target member must currently carry an evidence block, unless
  ``--allow-restored`` is given -- otherwise a stale patch silently overwrites a
  finished body;
* the old body is snapshotted to ``<patch>.evidence.json`` before it is
  replaced, because verification needs the evidence that the replacement
  deletes;
* the whole old body is replaced, which is also what removes the evidence block
  and any unfinished marker inside it.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..csharp import body as bodylib
from ..csharp import members as csm


@dataclass
class ApplyResult:
    written: int = 0
    skipped: int = 0
    problems: List[str] = field(default_factory=list)
    snapshot_path: Optional[Path] = None

    def as_dict(self) -> dict:
        return {
            "written": self.written,
            "skipped": self.skipped,
            "problems": self.problems,
            "snapshot": str(self.snapshot_path) if self.snapshot_path else "",
        }


def stale_marker_spans(
    text: str, member: csm.Member, policy: bodylib.BodyPolicy
) -> List[tuple[int, int, str]]:
    """Delete tool markers sitting in the comment block directly above a member.

    Markers written above the signature are outside the body, so replacing the
    body leaves them behind: the file then says ``PARTIALLY_RESTORED`` next to a
    finished member, and any reader (human or agent) trusts the comment.  Only
    the contiguous comment run is touched, so a marker separated by a blank line
    still belongs to the member above it.
    """
    spans: List[tuple[int, int, str]] = []
    cursor = text.rfind("\n", 0, member.header_start) + 1
    while cursor > 0:
        previous = text.rfind("\n", 0, cursor - 1) + 1
        line = text[previous:cursor]
        stripped = line.strip()
        if not stripped.startswith("//") or stripped.startswith("///"):
            break
        if policy.marker_re.match(stripped):
            spans.append((previous, cursor, ""))
        cursor = previous
    return spans


def apply_patch(
    patch_path: Path | str,
    src_dir: Path | str,
    backup_dir: Optional[Path | str] = None,
    dry_run: bool = False,
    allow_restored: bool = False,
    policy: bodylib.BodyPolicy = bodylib.DEFAULT_POLICY,
    snapshot: bool = True,
) -> ApplyResult:
    patch_path = Path(patch_path)
    src_dir = Path(src_dir)
    source_root = src_dir.resolve()
    entries = json.loads(patch_path.read_text(encoding="utf-8-sig"))
    if isinstance(entries, dict):
        entries = entries.get("entries", [])
    backup = Path(backup_dir) if backup_dir else None

    by_file: Dict[str, List[dict]] = {}
    for entry in entries:
        by_file.setdefault(entry["file"], []).append(entry)

    result = ApplyResult()
    evidence_snapshot: List[dict] = []

    for relative, file_entries in by_file.items():
        path = (src_dir / relative).resolve()
        if not path.is_relative_to(source_root):
            result.problems.append(f"INVALID_PATH {relative}")
            result.skipped += len(file_entries)
            continue
        if not path.is_file():
            result.problems.append(f"FILE_MISSING {relative}")
            result.skipped += len(file_entries)
            continue
        text = path.read_text(encoding="utf-8-sig")
        parsed = csm.parse(text)
        edits: List[tuple[int, int, str]] = []
        accepted = 0
        for entry in file_entries:
            member = parsed.find(
                entry["member"], int(entry.get("params", 0)), entry.get("signature")
            )
            if member is None:
                result.problems.append(f"MEMBER_MISSING {relative}::{entry['member']}")
                result.skipped += 1
                continue
            old_body = csm.body_of(member, text)
            if not policy.has_evidence(old_body) and not allow_restored:
                result.problems.append(f"NO_EVIDENCE {relative}::{entry['member']}")
                result.skipped += 1
                continue
            inner = bodylib.reindent(entry.get("body", ""), member.indent)
            new_body = "{\n" + inner + ("\n" if inner else "") + member.indent + "}"
            evidence_snapshot.append(
                {
                    "file": relative,
                    "member": entry["member"],
                    "params": int(entry.get("params", 0)),
                    "evidence": old_body,
                }
            )
            # ``body_end`` is exclusive (it points just past the closing brace),
            # the same convention ``body_of`` uses; adding one here ate the
            # blank line that separates the member from the next one.
            edits.append((member.body_start, member.body_end, new_body))
            edits.extend(stale_marker_spans(text, member, policy))
            accepted += 1
        if not edits:
            continue
        if backup:
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        for start, end, new_body in sorted(edits, reverse=True):
            text = text[:start] + new_body + text[end:]
        # One member may contribute several edits (its body plus stale markers
        # above it); the count is members, not edits.
        result.written += accepted
        if not dry_run:
            path.write_text(text, encoding="utf-8")

    if snapshot and evidence_snapshot and not dry_run:
        side = patch_path.with_suffix(".evidence.json")
        side.write_text(
            json.dumps(evidence_snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        result.snapshot_path = side
    return result


def write_patch(entries: List[dict], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def body_from_card(card: dict, body_text: str, note: str = "") -> dict:
    """Turn one model answer into a patch entry."""
    entry = {
        "file": card["file"],
        "member": card["member"],
        "params": card.get("params", 0),
        "body": body_text,
    }
    if card.get("signature"):
        entry["signature"] = card["signature"]
    if note:
        entry["note"] = note
    return entry
