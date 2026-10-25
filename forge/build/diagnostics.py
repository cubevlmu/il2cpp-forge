"""Compiler diagnostics: parse, de-duplicate, classify.

Two traps this module exists for:

* ``dotnet build -v q`` prints every error twice (once from the compiler, once
  from the MSBuild summary).  Counting raw lines doubles the number and makes
  progress look like regression.
* The total error count is a poor progress signal.  A tree can gain errors
  because a *new* body introduced deeper semantic checking, while the errors
  that actually block the compiler -- syntax-class ones -- go to zero.  Judge by
  syntax-class errors and by residue, not by the total.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<severity>error|warning)\s+(?P<code>CS\d{4}|[A-Z]{2,}\d+):\s*(?P<message>.*)$"
)
BARE_RE = re.compile(r"^(?P<severity>error|warning)\s+(?P<code>CS\d{4}):\s*(?P<message>.*)$")
SUMMARY_RE = re.compile(
    r"(?P<errors>\d+)\s+Error\(s\)|(?P<werrors>\d+)\s+Warning\(s\)"
)

# Errors that mean "this file cannot be parsed", as opposed to "this file does
# not type-check".  These must reach zero before any other number means
# anything, because the compiler aborts the semantic pass on them.
SYNTAX_CODES = {
    "CS1001", "CS1002", "CS1003", "CS1010", "CS1012", "CS1022", "CS1026",
    "CS1031", "CS1039", "CS1040", "CS1056", "CS1513", "CS1514", "CS1519",
    "CS1525", "CS1526", "CS1520", "CS0116", "CS8803", "CS8805", "CS8124",
    "CS8180", "CS1026", "CS1031", "CS1003",
}

# "Residue": decompiler text that survived into the tree.  Not a compiler
# concept -- a regex sweep over the sources, run alongside the build.
RESIDUE_PATTERNS = {
    "il2cpp_object": re.compile(r"\bIl2Cpp(?:Object|Class|String|Array)\b"),
    "sub_call": re.compile(r"\bsub_[0-9A-Fa-f]{6,}\b"),
    "arrow_field": re.compile(r"->(?:fields|klass)\b"),
    "ll_suffix": re.compile(r"\b\d+LL\b"),
    "int64_cast": re.compile(r"\(__int64\)"),
    "string_literal_id": re.compile(r"\bStringLiteral_\d+\b"),
    "byte_guard": re.compile(r"\bbyte_[0-9A-Fa-f]{6,}\b"),
    "native_helper": re.compile(r"NativeHelper"),
    "mechanical_marker": re.compile(r"MECHANICAL_TRANSLATION"),
}


@dataclass
class Diagnostic:
    file: str
    line: int
    column: int
    severity: str
    code: str
    message: str

    @property
    def syntax(self) -> bool:
        return self.code in SYNTAX_CODES

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "syntax": self.syntax,
        }


@dataclass
class BuildReport:
    diagnostics: List[Diagnostic] = field(default_factory=list)
    reported_errors: int = 0
    reported_warnings: int = 0
    raw_lines: int = 0
    exit_code: int = 0

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def syntax_errors(self) -> List[Diagnostic]:
        return [d for d in self.errors if d.syntax]

    def by_code(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for diagnostic in self.errors:
            counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    def as_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "errors": len(self.errors),
            "warnings": len(self.diagnostics) - len(self.errors),
            "reported_errors": self.reported_errors,
            "reported_warnings": self.reported_warnings,
            "syntax_errors": len(self.syntax_errors),
            "codes": self.by_code(),
            "syntax_files": sorted({d.file for d in self.syntax_errors})[:40],
        }


def parse_output(output: str) -> BuildReport:
    report = BuildReport()
    seen: set[tuple] = set()
    for raw in output.splitlines():
        report.raw_lines += 1
        line = raw.strip()
        if not line:
            continue
        match = DIAGNOSTIC_RE.match(line)
        if match:
            diagnostic = Diagnostic(
                file=match.group("file").strip(),
                line=int(match.group("line")),
                column=int(match.group("col")),
                severity=match.group("severity"),
                code=match.group("code"),
                message=match.group("message").strip(),
            )
        else:
            bare = BARE_RE.match(line)
            if not bare:
                summary = re.search(r"(\d+)\s+Error\(s\)", line)
                if summary:
                    report.reported_errors = max(report.reported_errors, int(summary.group(1)))
                warn_summary = re.search(r"(\d+)\s+Warning\(s\)", line)
                if warn_summary:
                    report.reported_warnings = max(
                        report.reported_warnings, int(warn_summary.group(1))
                    )
                continue
            diagnostic = Diagnostic(
                file="",
                line=0,
                column=0,
                severity=bare.group("severity"),
                code=bare.group("code"),
                message=bare.group("message").strip(),
            )
        key = (diagnostic.file, diagnostic.line, diagnostic.code, diagnostic.message)
        if key in seen:
            continue
        seen.add(key)
        report.diagnostics.append(diagnostic)
    return report


def strip_comments(text: str) -> str:
    """Blank out comments but keep string literals.

    Residue must be measured on *live code*: an evidence block legitimately
    quotes ``sub_52A76A0`` and ``->fields`` in a comment, and counting those
    makes a clean tree look contaminated.  Literals are kept because
    ``throw new NotImplementedException("MECHANICAL_TRANSLATION")`` only reveals
    itself inside one.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "*":
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i + 1 < n:
                out[i] = out[i + 1] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def scan_residue(src_dir: Path | str, suffixes: tuple[str, ...] = (".cs",)) -> Dict[str, int]:
    """Count decompiler residue in live code (comments excluded)."""
    src_dir = Path(src_dir)
    counts: Dict[str, int] = {name: 0 for name in RESIDUE_PATTERNS}
    for path in src_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        live = strip_comments(text)
        for name, pattern in RESIDUE_PATTERNS.items():
            counts[name] += len(pattern.findall(live))
    return counts


def gate(report: BuildReport, max_syntax_errors: int = 0, residue: Optional[Dict[str, int]] = None,
         max_residue: int = 0) -> tuple[bool, List[str]]:
    """Return ``(passed, reasons)`` for the configured gate."""
    reasons: List[str] = []
    if len(report.syntax_errors) > max_syntax_errors:
        reasons.append(
            f"syntax-class errors {len(report.syntax_errors)} > {max_syntax_errors}"
        )
    if residue is not None:
        total = sum(residue.values())
        if total > max_residue:
            worst = sorted(residue.items(), key=lambda item: -item[1])[:4]
            reasons.append(f"residue {total} > {max_residue} ({worst})")
    return (not reasons), reasons


def compare(before: BuildReport, after: BuildReport) -> dict:
    """Delta that distinguishes "worse" from "deeper"."""
    before_codes = before.by_code()
    after_codes = after.by_code()
    return {
        "errors_before": len(before.errors),
        "errors_after": len(after.errors),
        "delta": len(after.errors) - len(before.errors),
        "syntax_before": len(before.syntax_errors),
        "syntax_after": len(after.syntax_errors),
        "new_codes": {k: v for k, v in after_codes.items() if k not in before_codes},
        "fixed_codes": {k: before_codes[k] for k in before_codes if k not in after_codes},
    }
