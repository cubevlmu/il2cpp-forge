"""Body anatomy: separating "the C# that runs" from "the evidence that justifies it".

Every stage of the toolkit needs the same three views of a member body, and
every earlier implementation that took ``body[:index]`` got them wrong, because
an evidence block may sit either before or after the draft code.  The functions
here work on *block boundaries*, never on position:

``strip_evidence``  body minus the evidence block and minus marker/boilerplate
                    comments, outer braces still attached
``inner_of``        the same with the member's own braces removed (patch format)
``code_of``         only the lines that actually execute
``evidence_of``     the evidence block text itself, for audit and re-queue

The banner is configurable (``project.evidence_banner``) but defaults to
``UNRESTORED EVIDENCE``, which is what this family of tools has always written.
Boundaries are matched as *banner* lines -- ``// ... UNRESTORED EVIDENCE`` at
the start of a comment -- because a prose mention inside the mechanical
translation header (``// Original C evidence preserved in UNRESTORED EVIDENCE
block above.``) used to re-open the block and swallow the statements after it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

DEFAULT_BANNER = "UNRESTORED EVIDENCE"

# Markers that mean "unfinished".  ``INFER:`` is intentionally absent: it is an
# accepted inference note and must survive a strip so the reviewer can see it.
UNFINISHED_WORDS = (
    "PARTIAL",
    "DRAFT",
    "ASSUMED",
    "INFER_RESTORE",
    "INFERRED",
    "TODO",
    "FIXME",
    "UNFINISHED",
    "WIP",
    "NOT_SUPPORTED",
    "RECOVERY_DRAFT",
)

BOILER_SUBSTRINGS = (
    "Native evidence below is commentary only",
    "and its existing C# body is preserved.",
    "Native C pseudocode follows",
    "Original unresolved reason:",
    "Original required evidence:",
    "Original C evidence preserved in",
    "the C# above is an incomplete attempt",
    "Finish it against the native evidence",
    "the C# above is a bare stub",
)

PLACEHOLDER_RE = re.compile(
    r"NotImplementedException|MECHANICAL_TRANSLATION|NotSupportedException\(\s*\"(?:Async native logic|native logic still unresolved)"
)

# Text that only exists in decompiler output, never in compilable C#.
MECHANICAL_RE = re.compile(
    r"Il2CppObject|Il2CppClass|Il2CppString|Il2CppArray|NativeHelper|"
    r"\bsub_[0-9A-Fa-f]{6,}\b|->fields|->klass|\b0LL\b|\(__int64\)|"
    r"\bStringLiteral_\d+\b|\bLODWORD\(|\bHIDWORD\(|\bbyte_[0-9A-F]{6,}\b"
)

# Reflection used to fake dispatch: passes every residue check, dies under
# IL2CPP stripping.  ``BindingFlags`` alone is not a signal (real code uses it).
SCAFFOLDING_RE = re.compile(
    r"MetadataToken|GetMethods\(\)\s*\.|GetField\(\s*\"_|MissingMethodException|"
    r"MissingFieldException|GetRuntimeMethods\(\)"
)

TRIVIAL_RE = re.compile(
    r"^\s*\{\s*(?:return\s+(?:default|default\([^)]*\)|null|0|false|-1)\s*;)?\s*\}\s*$", re.S
)
LEGIT_EMPTY_NAMES = re.compile(
    r"^(?:\.?ctor|Dispose|Installer|Install|Uninstall|Awake|Start|OnDestroy|OnEnable|OnDisable)$"
)

STATUS_RE = re.compile(r"status=(\w+)")
RVA_RE = re.compile(r"rva=(0x[0-9A-Fa-f]+)")
SOURCE_RE = re.compile(r"source:\s*([\w./\\-]+\.c)")
TASK_RE = re.compile(r"task_id=(\S+)")


@dataclass
class BodyPolicy:
    """Which comments count as unfinished markers."""

    banner: str = DEFAULT_BANNER
    unfinished_words: tuple[str, ...] = UNFINISHED_WORDS
    accepted_words: tuple[str, ...] = ("INFER", "INFERRED_DETAIL")

    @property
    def start_re(self) -> re.Pattern[str]:
        return re.compile(
            r"^\s*//[\s\-=*]*" + re.escape(self.banner) + r"\b", re.IGNORECASE
        )

    @property
    def end_re(self) -> re.Pattern[str]:
        return re.compile(
            r"^\s*//[\s\-=*]*(?:END\s+)?" + re.escape(self.banner) + r"\b",
            re.IGNORECASE,
        )

    @property
    def marker_re(self) -> re.Pattern[str]:
        words = "|".join(re.escape(w) for w in self.unfinished_words)
        return re.compile(r"^\s*//\s*\w*(?:" + words + r")\w*")

    def has_evidence(self, body: str) -> bool:
        return any(self.start_re.match(line) for line in body.splitlines())


DEFAULT_POLICY = BodyPolicy()


def strip_evidence(body: str, policy: BodyPolicy = DEFAULT_POLICY) -> str:
    """Body without the evidence block, marker comments and boilerplate."""
    out: list[str] = []
    in_ev = False
    for line in body.splitlines():
        if not in_ev and policy.start_re.match(line):
            in_ev = True
            continue
        if in_ev:
            if policy.end_re.match(line) and not policy.start_re.match(line):
                in_ev = False
            continue
        stripped = line.strip()
        if stripped.startswith("//"):
            if policy.marker_re.match(line):
                continue
            if any(b in stripped for b in BOILER_SUBSTRINGS):
                continue
            if stripped == "//":
                continue
        out.append(line)
    return "\n".join(out)


def inner_of(body: str, policy: BodyPolicy = DEFAULT_POLICY) -> str:
    """``strip_evidence`` with the member's own outer braces removed."""
    text = strip_evidence(body, policy).strip()
    if text.startswith("{"):
        text = text[1:]
    if text.rstrip().endswith("}"):
        text = text.rstrip()[:-1]
    return text


def code_of(body: str, policy: BodyPolicy = DEFAULT_POLICY) -> list[str]:
    """Statements that actually run: no comments, no bare braces."""
    out = []
    for line in strip_evidence(body, policy).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if not stripped.strip("{}").strip():
            continue
        out.append(line)
    return out


def evidence_of(body: str, policy: BodyPolicy = DEFAULT_POLICY) -> str:
    """The evidence block text, for audit and re-queue."""
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        if policy.start_re.match(line):
            start = index
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if policy.end_re.match(lines[index]):
            end = index + 1
            break
    return "\n".join(lines[start:end])


def evidence_meta(body: str, policy: BodyPolicy = DEFAULT_POLICY) -> dict:
    """``status`` / ``rva`` / ``source`` / ``task_id`` recorded in the evidence."""
    block = evidence_of(body, policy)
    meta: dict = {}
    for key, regex in (
        ("status", STATUS_RE),
        ("rva", RVA_RE),
        ("source", SOURCE_RE),
        ("task_id", TASK_RE),
    ):
        match = regex.search(block)
        if match:
            meta[key] = match.group(1)
    return meta


@dataclass
class Verdict:
    issues: list[str] = field(default_factory=list)
    coverage: float | None = None
    anchors_total: int = 0
    anchors_hit: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


def is_legitimately_empty(name: str, owner: str = "") -> bool:
    """Constructors and Unity lifecycle callbacks are often genuinely empty.

    The member parser reports a constructor under its *class* name, so the owner
    has to be known or every empty ``Foo() { }`` looks like a silent closure.
    """
    if LEGIT_EMPTY_NAMES.match(name or ""):
        return True
    if owner and name and name == owner.split(".")[-1]:
        return True
    return False


def classify(
    body: str,
    policy: BodyPolicy = DEFAULT_POLICY,
    *,
    name: str = "",
    owner: str = "",
    coverage: float | None = None,
    min_coverage: float = 0.6,
    anchors_total: int = 0,
    anchors_hit: int = 0,
) -> Verdict:
    """Judge one body.  ``issues`` empty means "looks finished"."""
    issues: list[str] = []
    live = "\n".join(code_of(body, policy))
    if policy.has_evidence(body):
        issues.append("EVIDENCE")
    if any(policy.marker_re.match(line) for line in body.splitlines()):
        issues.append("MARKER")
    if PLACEHOLDER_RE.search(live):
        issues.append("PLACEHOLDER")
    if MECHANICAL_RE.search(live):
        issues.append("MECHANICAL")
    if SCAFFOLDING_RE.search(live):
        issues.append("SCAFFOLDING")
    if not live.strip() and not is_legitimately_empty(name, owner):
        issues.append("EMPTY")
    if coverage is not None and anchors_total:
        if anchors_hit == 0:
            issues.append("SILENT_CLOSURE")
        elif coverage < min_coverage:
            issues.append(f"LOW_COVERAGE:{coverage:.2f}")
    return Verdict(
        issues=issues,
        coverage=coverage,
        anchors_total=anchors_total,
        anchors_hit=anchors_hit,
    )


def indent_unit(indent: str) -> str:
    """The per-level indent that matches a member's own indentation.

    Assuming a tab turns an eight-space member into ``        \\t`` -- two
    indentation schemes in one line, which no formatter will fix.
    """
    if "\t" in indent:
        return "\t"
    if not indent:
        return "    "
    return " " * (4 if len(indent) % 4 == 0 else 2)


def reindent(body: str, indent: str, unit: Optional[str] = None) -> str:
    """Re-indent ``body`` so its first level sits one ``unit`` under ``indent``."""
    unit = unit or indent_unit(indent)
    lines = [line.rstrip() for line in body.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    base = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    inner = indent + unit
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        out.append(inner + line[base:])
    return "\n".join(out)


def iter_markers(text: str, policy: BodyPolicy = DEFAULT_POLICY) -> Iterable[str]:
    for line in text.splitlines():
        if policy.marker_re.match(line):
            yield line.strip()
