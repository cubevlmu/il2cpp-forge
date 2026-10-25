"""Human-readable status: one page that says what is left and what is risky."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..restore.queue import Queue


def _table(rows: List[List[str]], header: List[str]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def queue_markdown(
    queue: Queue,
    title: str = "Reconstruction status",
    index_summary: Optional[dict] = None,
    build_summary: Optional[dict] = None,
    verify_summary: Optional[dict] = None,
) -> str:
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"_generated {datetime.datetime.now():%Y-%m-%d %H:%M}_")
    lines.append("")

    total = max(queue.members, 1)
    counts = queue.counts
    rows = []
    for kind in ("partial", "stub", "marker_only", "empty_silent", "legit_empty", "restored"):
        value = counts.get(kind, 0)
        rows.append([kind, value, f"{value / total:.2%}"])
    rows.append(["**total**", queue.members, ""])
    lines.extend(_table(rows, ["kind", "members", "share"]))
    lines.append("")
    open_total = len(queue.open_entries())
    lines.append(
        f"Open members: **{open_total}** of {queue.members} across {queue.files} files."
    )
    if counts.get("marker_only"):
        lines.append(
            f"- `marker_only` = {counts['marker_only']}: evidence was deleted but an "
            "unfinished marker survived. These do not appear in evidence-based counts."
        )
    if counts.get("empty_silent"):
        lines.append(
            f"- `empty_silent` = {counts['empty_silent']}: empty body with no marker. "
            "Only the native code can tell these from legitimately empty members."
        )
    lines.append("")

    if index_summary:
        lines.append("## Index")
        rows = [
            ["types", index_summary.get("types", 0)],
            ["methods", index_summary.get("methods", 0)],
            ["target methods", index_summary.get("target_methods", 0)],
            ["joined to native", index_summary.get("methods_with_native", 0)],
            ["  by script.json", index_summary.get("joined_by_script", 0)],
            ["  by mangled name", index_summary.get("joined_by_name", 0)],
            ["  by address", index_summary.get("joined_by_address", 0)],
            ["image base", index_summary.get("image_base_hex", "0x0")],
            ["native functions", index_summary.get("native_functions", 0)],
            ["string literals", index_summary.get("literals", 0)],
        ]
        lines.extend(_table(rows, ["metric", "value"]))
        lines.append("")

    if verify_summary:
        lines.append("## Verification")
        rows = [
            ["checked", verify_summary.get("checked", 0)],
            ["ok", verify_summary.get("ok", 0)],
            ["failed", verify_summary.get("failed", 0)],
        ]
        lines.extend(_table(rows, ["metric", "value"]))
        if verify_summary.get("by_issue"):
            lines.append("")
            lines.extend(
                _table(
                    [[k, v] for k, v in verify_summary["by_issue"].items()],
                    ["issue", "count"],
                )
            )
        lines.append("")

    if build_summary:
        lines.append("## Build")
        rows = [
            ["exit code", build_summary.get("exit_code", 0)],
            ["errors", build_summary.get("errors", 0)],
            ["syntax-class errors", build_summary.get("syntax_errors", 0)],
            ["warnings", build_summary.get("warnings", 0)],
        ]
        lines.extend(_table(rows, ["metric", "value"]))
        codes = build_summary.get("codes") or {}
        if codes:
            lines.append("")
            lines.extend(
                _table([[code, count] for code, count in list(codes.items())[:12]], ["code", "count"])
            )
        residue = build_summary.get("residue") or {}
        if residue:
            lines.append("")
            lines.append(
                "Residue: "
                + ", ".join(f"{k}={v}" for k, v in residue.items() if v)
                if any(residue.values())
                else "Residue: none"
            )
        lines.append("")

    if queue.entries:
        lines.append("## Next work (highest value first)")
        rows = []
        for entry in sorted(
            queue.open_entries(), key=lambda e: (0 if e.native_name else 1, e.code_lines)
        )[:25]:
            rows.append(
                [
                    entry.kind,
                    f"`{entry.file}`",
                    f"{entry.member}/{entry.params}",
                    entry.native_name or "-",
                ]
            )
        lines.extend(_table(rows, ["kind", "file", "member", "native"]))
        lines.append("")

    return "\n".join(lines) + "\n"


def write(path: Path | str, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
