"""Line-oriented C# member locator.

Decompiled trees are machine formatted: one member per block, tabs or four
spaces, no exotic syntax.  A line scan with brace matching is therefore both
simpler and more predictable than a real grammar -- and it is robust against
the half-finished bodies this toolkit exists to finish.

Handled shapes (each one broke an earlier naive implementation somewhere):

* method / constructor declarations, including parameter lists wrapped over
  several lines by a formatter;
* constructor initialisers ``: base(...)`` / ``: this(...)``;
* property accessors ``get``/``set``/``init``, reported as ``get_Name`` /
  ``set_Name`` so they can be queued and patched like methods;
* inline bodies ``void Foo() { }`` and one-line ``{ return x; }``;
* tuple-typed properties, whose header would otherwise parse as a call;
* explicit interface implementations ``void IFoo.Bar()`` vs. a call site.

Comments, strings and char literals are masked before scanning so braces inside
them never disturb the offset arithmetic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CONTROL = {
    "if", "for", "foreach", "while", "switch", "catch", "lock", "using", "try",
    "do", "else", "fixed", "checked", "unchecked", "return", "new", "await",
    "yield", "throw", "when", "base", "this", "get", "set", "init", "add",
    "remove", "where", "operator", "delegate",
}

MODIFIERS = {
    "public", "private", "protected", "internal", "static", "readonly", "virtual",
    "override", "sealed", "abstract", "partial", "async", "extern", "unsafe",
    "const", "event", "implicit", "explicit", "ref", "out", "in", "params",
    "required", "volatile", "fixed", "new", "this", "base",
}

TYPE_LINE = re.compile(r"\b(class|struct|interface|enum|record)\b")
ACCESSOR_LINE = re.compile(r"^(?:(?:private|protected|internal|public)\s+)?(get|set|init)\s*$")
ACCESSOR_INLINE = re.compile(
    r"^(?:(?:private|protected|internal|public)\s+)?(get|set|init)\s*\{"
)
TYPE_DECL = re.compile(
    r"^[ \t]*(?:(?:public|internal|private|protected|sealed|abstract|partial|static|unsafe|new)\s+)*"
    r"(?:class|struct|record|interface)\s+([\w\.<>]+)",
    re.M,
)


def mask(text: str) -> str:
    """Blank out comments and literals, preserving offsets and newlines."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i + 1 < n:
                out[i] = out[i + 1] = " "
            i += 2
        elif c == '"' or (c in "@$" and i + 1 < n and text[i + 1] in '@"$'):
            start = i
            while i < n and text[i] != '"':
                i += 1
            verbatim = "@" in text[start:i]
            i += 1
            while i < n:
                if not verbatim and text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    if verbatim and i + 1 < n and text[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            for k in range(start, min(i, n)):
                if text[k] != "\n":
                    out[k] = " "
        elif c == "'":
            start = i
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            for k in range(start, min(i, n)):
                if text[k] != "\n":
                    out[k] = " "
        else:
            i += 1
    return "".join(out)


def split_top(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in text:
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


def _param_inner(signature: str) -> str | None:
    start = signature.find("(")
    if start == -1:
        return None
    depth, end = 0, -1
    for position in range(start, len(signature)):
        char = signature[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = position
                break
    if end == -1:
        return None
    return signature[start + 1 : end]


def param_count(signature: str) -> int:
    inner = _param_inner(signature)
    if inner is None:
        return -1
    if not inner.strip():
        return 0
    return len([p for p in split_top(inner) if p.strip()])


def param_types(signature: str) -> list[str]:
    """Normalised parameter type list, ignoring names, attributes and modifiers."""
    inner = _param_inner(signature)
    if inner is None or not inner.strip():
        return []
    types = []
    for part in split_top(inner):
        cleaned = re.sub(r"\[[^\]]*\]", "", part).strip()
        cleaned = re.sub(r"\b(?:in|ref|out|params|this)\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        tokens = [t for t in cleaned.split() if t]
        if len(tokens) >= 2:
            types.append(" ".join(tokens[:-1]))
        elif tokens:
            types.append(tokens[0])
    return types


@dataclass
class Member:
    name: str
    params: int
    header_start: int
    body_start: int
    body_end: int
    is_expression: bool = False
    indent: str = ""
    signature: str = ""

    @property
    def key(self) -> str:
        return f"{self.name}/{self.params}"


@dataclass
class FileMembers:
    text: str
    masked: str
    members: list[Member] = field(default_factory=list)

    def find(self, name: str, params: int, signature: str | None = None) -> Member | None:
        hits = [m for m in self.members if m.name == name and m.params == params]
        if len(hits) > 1 and signature:
            wanted = param_types(signature)
            narrowed = [m for m in hits if param_types(m.signature) == wanted]
            if narrowed:
                hits = narrowed
        if len(hits) == 1:
            return hits[0]
        if hits:
            return hits[0]
        hits = [m for m in self.members if m.name == name]
        if len(hits) == 1:
            return hits[0]
        return None

    def line_of(self, member: Member) -> int:
        return self.text.count("\n", 0, member.header_start) + 1


def _decl_name(header: str) -> tuple[str, int] | None:
    """Return ``(name, param_count)`` when ``header`` is a member declaration."""
    stripped = header.strip()
    if not stripped or stripped.startswith("[") or "=>" in stripped:
        return None
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    if stripped.startswith(("return", "await", "throw", "new ", "if ", "else")):
        return None
    if TYPE_LINE.search(stripped):
        return None
    if "(" not in stripped:
        return None
    head = stripped.split("(")[0]
    name_match = re.search(r"([A-Za-z_]\w*)\s*(?:<[^<>]*>)?\s*$", head)
    if not name_match:
        return None
    name = name_match.group(1)
    if name in CONTROL or name in MODIFIERS:
        return None
    prefix = head[: name_match.start()].strip()
    if "=" in prefix:
        return None
    if prefix.endswith("."):
        qualifier = prefix[:-1].strip()
        tokens = qualifier.split()
        last = tokens[-1] if tokens else ""
        if not (len(tokens) >= 2 and (last[:1].isupper() or "::" in last)):
            return None
    return name, param_count(stripped)


def _join_declaration(lines: list[str], start: int) -> tuple[int, str]:
    """Join a declaration whose parameter list wraps across lines."""
    parts = [lines[start].rstrip("\r\n")]
    depth = parts[0].count("(") - parts[0].count(")")
    last = start
    while depth > 0 and last + 1 < len(lines):
        last += 1
        piece = lines[last].rstrip("\r\n")
        parts.append(piece)
        depth += piece.count("(") - piece.count(")")
    return last, " ".join(part.strip() for part in parts)


def _skip_initializer(lines: list[str], start: int) -> int:
    """Index just past a constructor's ``: base(...)`` / ``: this(...)`` clause."""
    index = start
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return start
    text = lines[index].strip()
    if not text.startswith(":") or not re.match(r":\s*(base|this)\s*\(", text):
        return start
    depth = 0
    while index < len(lines):
        for char in lines[index]:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        index += 1
        if depth <= 0:
            break
    return index


def _property_name(lines: list[str], index: int) -> str | None:
    """Name of the property whose accessor sits on ``lines[index]``."""
    accessor_indent = len(lines[index]) - len(lines[index].lstrip())
    for back in range(index - 1, max(index - 12, -1), -1):
        text = lines[back].strip()
        if not text or text.startswith("//") or text.startswith("["):
            continue
        if text in ("{", "}"):
            continue
        indent = len(lines[back]) - len(lines[back].lstrip())
        if indent >= accessor_indent:
            continue
        if "(" in text or "=" in text or ";" in text or "{" in text:
            return None
        match = re.search(r"([A-Za-z_]\w*)\s*$", text)
        return match.group(1) if match else None
    return None


def parse(text: str) -> FileMembers:
    masked = mask(text)
    result = FileMembers(text=text, masked=masked)
    lines = masked.splitlines(keepends=True)
    offsets = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line)
    offsets.append(running)

    i = 0
    while i < len(lines):
        raw = lines[i]
        body = raw.rstrip("\r\n")
        stripped = body.strip()
        if not stripped or stripped.startswith("[") or stripped.startswith("//"):
            i += 1
            continue
        accessor = ACCESSOR_LINE.match(stripped)
        inline = ACCESSOR_INLINE.match(stripped) if not accessor else None
        if accessor or inline:
            owner = _property_name(lines, i)
            open_at = -1
            if inline:
                open_at = offsets[i] + lines[i].index("{")
            else:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].strip().startswith("{"):
                    open_at = offsets[j] + lines[j].index("{")
            if owner and open_at != -1:
                close_at = _match_brace(masked, open_at)
                if close_at != -1:
                    keyword = (accessor or inline).group(1)
                    result.members.append(
                        Member(
                            name=f"{keyword}_{owner}",
                            params=0,
                            header_start=offsets[i],
                            body_start=open_at,
                            body_end=close_at + 1,
                            indent=re.match(r"[ \t]*", body).group(0),
                            signature=text[offsets[i] : open_at].strip(),
                        )
                    )
                    i = _line_index(offsets, close_at) + 1
                    continue
        last, joined = _join_declaration(lines, i)
        candidate = joined
        brace_at = candidate.find("{")
        brace_inline = brace_at != -1
        if brace_inline:
            candidate = candidate[:brace_at].rstrip()
        decl = _decl_name(candidate)
        if decl and brace_inline:
            open_at = masked.index("{", offsets[i])
            close_at = _match_brace(masked, open_at)
            if close_at != -1:
                result.members.append(
                    Member(
                        name=decl[0],
                        params=decl[1],
                        header_start=offsets[i],
                        body_start=open_at,
                        body_end=close_at + 1,
                        indent=re.match(r"[ \t]*", body).group(0),
                        signature=text[offsets[i] : open_at].strip(),
                    )
                )
                i = _line_index(offsets, close_at) + 1
                continue
        if decl and not brace_inline:
            j = _skip_initializer(lines, last + 1)
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith("{"):
                open_at = offsets[j] + lines[j].index("{")
                close_at = _match_brace(masked, open_at)
                if close_at != -1:
                    result.members.append(
                        Member(
                            name=decl[0],
                            params=decl[1],
                            header_start=offsets[i],
                            body_start=open_at,
                            body_end=close_at + 1,
                            indent=re.match(r"[ \t]*", body).group(0),
                            signature=text[offsets[i] : open_at].strip(),
                        )
                    )
                    i = _line_index(offsets, close_at) + 1
                    continue
        i += 1
    return result


def _line_index(offsets: list[int], position: int) -> int:
    low, high = 0, len(offsets) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if offsets[mid] <= position:
            low = mid
        else:
            high = mid - 1
    return low


def _match_brace(masked: str, open_at: int) -> int:
    depth = 0
    for position in range(open_at, len(masked)):
        char = masked[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return position
    return -1


def body_of(member: Member, text: str) -> str:
    return text[member.body_start : member.body_end]


def load(path: Path) -> FileMembers:
    return parse(Path(path).read_text(encoding="utf-8-sig"))


def enclosing_type(text: str, pos: int) -> str:
    """Name of the last class/struct declared before ``pos``.

    A constructor is reported under its class name, so the only way to tell
    ``Foo.Foo() { }`` from an empty method called ``Foo`` is to compare against
    the enclosing declaration.
    """
    found = ""
    for match in TYPE_DECL.finditer(text, 0, pos):
        found = match.group(1)
    return found


def shape_of(name: str, owner: str = "") -> str:
    if name == "MoveNext":
        return "MoveNext"
    if name in (".ctor", "cctor") or (owner and name == owner):
        return "ctor"
    if name.startswith("get_"):
        return "getter"
    if name.startswith("set_"):
        return "setter"
    if "g__" in name:
        return "local function"
    return "method"
