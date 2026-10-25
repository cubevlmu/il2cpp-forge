"""Decompiled-C corpus: locate functions, split them into statements, reduce them.

Decompiler output cannot be filtered line by line: one call spans ten lines of
arguments, and the class-init guard is a multi-line ``if`` block that has to go
as a unit.  The reduction here is *reachability from behaviour*:

1. Tokenise into statements (split on ``;`` at paren depth 0; ``{``/``}`` are
   their own tokens) and build a block tree.
2. A statement is a seed when it mentions a business call
   (``Some_Type__Method(``), a business field (``fields.name``) or a string
   literal.  Everything else -- locals, class init, write barriers, vtable and
   interface-offset lookups, null/bounds throws, state-machine bookkeeping,
   struct copies between temporaries -- is plumbing.
3. Keep seeds plus any control block that contains a seed, so guards survive
   with their bodies.  Drop every other block whole.

The result is typically 10-60 statements for a 500-line decompilation and maps
close to one-to-one onto the original C#.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from ..native.helpers import HelperTable

DEFINITION = re.compile(
    r"^[ \t]*(?:[A-Za-z_][\w:<>,\*&\[\] ]*?)[ \t\*]+([A-Za-z_]\w*__[A-Za-z_]\w*)[ \t]*\(",
    re.M,
)
ANY_DEFINITION = re.compile(r"^([A-Za-z_]\w*)[ \t]*\([^;]*$", re.M)
ADDRESS_BANNER = re.compile(r"^//-+[ \t]*\(([0-9A-Fa-f]{6,})\)", re.M)

# ``Foo_Type__Bar_d__4__MoveNext(&state, method)`` -- the state machine body of
# ``Bar``.  The real logic of an async method lives here, not in the builder
# trampoline, so every coverage measurement has to follow this call.
MOVENEXT = re.compile(r"\b([A-Za-z_]\w*)__(\w+)_d__(\d+)__MoveNext\s*\(")

CONTROL = re.compile(r"^(?:if|else|for|while|do|switch|try|catch|finally|lock|using)\b")
# A control head that owns exactly one following token, because Hex-Rays leaves
# the braces off single-statement branches.
CONTROL_PREFIX = re.compile(
    r"^(?P<kw>else\s+if|else|if|for|while|switch|do|try|catch|finally|lock|using)\b",
    re.S,
)
CONTROL_HEAD = re.compile(
    r"^(?:else\s+if\s*\(.*\)|else|if\s*\(.*\)|for\s*\(.*\)|while\s*\(.*\)|"
    r"switch\s*\(.*\)|do|try|finally)$",
    re.S,
)
LABEL = re.compile(r"^(?:LABEL_\d+|default)\s*:")
LABEL_PREFIX = re.compile(r"^(?P<label>(?:LABEL_\d+|default)\s*:)\s*(?P<rest>.*)$", re.S)
CLASS_INIT_GUARD = re.compile(r"if\s*\(\s*\(\s*byte_[0-9A-Fa-f]+\s*&\s*1\s*\)\s*==\s*0\s*\)")
DECL_ONLY = re.compile(
    r"^\s*(?:[A-Za-z_][\w:<>,\.\[\] \*&]*\s+)?\*?\s*[\w\[\]]+\s*;\s*(?://.*)?$"
)

BUSINESS_CALL = re.compile(r"\b([A-Za-z_]\w*)__(\w+)\s*\(")
FIELD = re.compile(r"\bfields\.(\w+)")
LITERAL = re.compile(r'"(?:[^"\\]|\\.)+"|StringLiteral_\d+')
PLUMBING_SHAPE = re.compile(
    r"_TypeInfo|_2\.rank|interfaceOffsets|"
    r"AwaitUnsafeOnCompleted|SetResult|GetResult|get_IsCompleted|get_IsFaulted|"
    r"OnCompleted|GetStatus|__1__state|__u__[12]|__t__builder|cctor_finished|"
    r"fields\.__\d+__|fields\._\d+__\d+|fields\.\w+_\d+__\d+|__\d+__this|"
    r"\b_\d+__state\b|"
    r"\bbyte_[0-9A-F]{6,}\b|\+ 30[0-9]\)|\+ 19[0-9]\)"
)

# A virtual call is spelled ``((void (__fastcall *)(A, B))obj->klass->vtable.
# _75_set_text.methodPtr)(obj, b, obj->klass->vtable._75_set_text.method)``.
# That is *not* plumbing: ``set_text`` is the behaviour.  Only the slots every
# object carries are noise.
VTABLE_SLOT = re.compile(r"->klass->vtable\._(\d+)_(\w+)\.methodPtr")
VTABLE_PLUMBING = frozenset(
    {
        "GetType", "Equals", "GetHashCode", "ToString", "Finalize",
        "MemberwiseClone", "Dispose", "MoveNext", "GetResult", "SetResult",
        "get_IsCompleted", "get_IsFaulted", "OnCompleted", "GetStatus",
        "AwaitUnsafeOnCompleted", "SetException", "GetAwaiter",
    }
)
_VTABLE_CAST = re.compile(r"^\(\s*\(\s*void\s*\(\s*__fastcall\s*\*\s*\)\s*\([^)]*\)\s*\)\s*")
_VTABLE_HEAD = re.compile(r"^(\w+)->klass->vtable\._\d+_(\w+)\.methodPtr\s*\)\s*\(\s*\1\s*,\s*")
_VTABLE_TAIL = re.compile(r",\s*\w+->klass->vtable\._\d+_\w+\.method\s*\)\s*$")

# ``v241.fields.dist`` is a slot of a decompiler temporary, not a field of any
# managed object; a statement made only of those is struct shuffling.
# Hex-Rays uses ``.`` for value temporaries and ``->`` for pointer ones; both
# spellings are the same struct shuffling and must be filtered alike.
TEMP_FIELD = re.compile(r"\bv\d+\s*(?:\.|->)\s*fields\.\w+")
IF_HEAD = re.compile(r"^if\s*\(")
ELSE_HEAD = re.compile(r"^else\b")


# ---------------------------------------------------------------------------
# Corpus index
# ---------------------------------------------------------------------------
@dataclass
class FunctionRef:
    name: str
    path: Path
    address: Optional[int] = None


class FunctionIndex:
    """``Type__Method`` -> file, for a directory of decompiled C files.

    Scanning a real corpus (tens of thousands of files, hundreds of thousands of
    functions) takes minutes, so the name table can be cached to a TSV next to
    the project index.  The cache is invalidated by file count and newest mtime,
    which is cheap to compute and catches every regeneration that matters.
    """

    CACHE_VERSION = "1"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.functions: Dict[str, FunctionRef] = {}
        self.by_address: Dict[int, str] = {}
        self._cache: Dict[Path, str] = {}
        self._movenext: Dict[str, str] = {}

    @classmethod
    def build(
        cls,
        root: Path | str,
        suffixes: tuple[str, ...] = (".c", ".cpp"),
        cache_file: Path | str | None = None,
        reuse: bool = True,
    ) -> "FunctionIndex":
        root = Path(root)
        index = cls(root)
        files = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.lower() in suffixes
        ]
        stamp = cls._stamp(files)
        cache_path = Path(cache_file) if cache_file else None
        if cache_path and reuse and cache_path.is_file():
            if index._load_cache(cache_path, stamp, root):
                return index
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # The corpus is far larger than RAM is willing to spare, so only the
            # name -> file mapping is kept; text is re-read on demand.
            addresses = {}
            for match in ADDRESS_BANNER.finditer(text):
                addresses[match.end()] = int(match.group(1), 16)
            for match in DEFINITION.finditer(text):
                name = match.group(1)
                if name not in index.functions:
                    address = None
                    for offset, value in addresses.items():
                        if offset <= match.start():
                            address = value
                        else:
                            break
                    index.functions[name] = FunctionRef(name, path, address)
                    if address is not None and address not in index.by_address:
                        index.by_address[address] = name
        if cache_path:
            index._save_cache(cache_path, stamp)
        return index

    # -- cache ------------------------------------------------------------
    @staticmethod
    def _stamp(files: List[Path]) -> str:
        newest = 0.0
        for path in files:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        return f"{len(files)}|{newest:.0f}"

    def _load_cache(self, cache_path: Path, stamp: str, root: Path) -> bool:
        try:
            with cache_path.open("r", encoding="utf-8") as handle:
                header = handle.readline().strip().split("\t")
                if len(header) < 3 or header[0] != f"#forge-functions {self.CACHE_VERSION}":
                    return False
                if f"{header[1]}|{header[2]}" != stamp:
                    return False
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    name, relative, address = parts[0], parts[1], parts[2]
                    value = int(address, 16) if address else None
                    self.functions[name] = FunctionRef(name, root / relative, value)
                    if value is not None and value not in self.by_address:
                        self.by_address[value] = name
            return bool(self.functions)
        except (OSError, ValueError):
            self.functions.clear()
            self.by_address.clear()
            return False

    def _save_cache(self, cache_path: Path, stamp: str) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with cache_path.open("w", encoding="utf-8", newline="\n") as handle:
                count, newest = stamp.split("|")
                handle.write(f"#forge-functions {self.CACHE_VERSION}\t{count}\t{newest}\n")
                for name, ref in self.functions.items():
                    relative = ref.path.relative_to(self.root) if self.root else ref.path.name
                    address = f"{ref.address:X}" if ref.address is not None else ""
                    handle.write(f"{name}\t{str(relative).replace(chr(92), '/')}\t{address}\n")
        except OSError:
            pass

    # -- lookups ----------------------------------------------------------
    def get(self, name: str) -> Optional[FunctionRef]:
        return self.functions.get(name)

    def __len__(self) -> int:
        return len(self.functions)

    def text_of(self, path: Path) -> str:
        if path not in self._cache:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[path] = path.read_text(encoding="utf-8", errors="replace")
        return self._cache[path]

    def read(self, name: str) -> str:
        """The full text of one function, braces matched."""
        ref = self.get(name)
        if not ref:
            return ""
        return extract_function(self.text_of(ref.path), name)

    def find_movennext(self, owner: str, method: str) -> Optional[str]:
        """Name of ``method``'s state-machine body, if the corpus has one."""
        if not self._movenext:
            pattern = re.compile(r"^(?P<prefix>.+_d__\d+)__MoveNext$")
            for name in self.functions:
                match = pattern.match(name)
                if match:
                    self._movenext.setdefault(match.group("prefix"), name)
        prefix = f"{owner}__{method}"
        for key, name in self._movenext.items():
            if key.startswith(prefix + "_d__"):
                return name
        return None

    def sample(self, count: int) -> Iterator[str]:
        """Evenly spaced sample across the corpus.

        Taking the first N names biases every statistic towards whichever
        directory sorts first (usually a third-party SDK), which is exactly the
        code whose helper usage does not represent the game's.
        """
        names = list(self.functions)
        if not names:
            return
        stride = max(1, len(names) // max(count, 1))
        for position in range(0, len(names), stride):
            if count <= 0:
                return
            count -= 1
            yield self.read(names[position])


def extract_function(text: str, name: str) -> str:
    """Slice one function definition out of ``text`` by brace matching.

    The definition may be preceded by an address banner and by a return type on
    its own line; the body may contain braces inside strings, comments and
    character literals, so those are skipped rather than counted.
    """
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    for match in pattern.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line = text[line_start : match.end()]
        if line.lstrip().startswith("//"):
            continue
        brace = _find_open_brace(text, match.end())
        if brace == -1:
            continue
        end = _match_brace(text, brace)
        if end == -1:
            continue
        # Include the declaration lines (return type, banner) for readability.
        # Keep the address banner and any comment lines that introduce the
        # function, so the slice can be traced back to the disassembly.
        decl_start = line_start
        while decl_start > 0:
            previous_start = text.rfind("\n", 0, decl_start - 1) + 1
            if not text[previous_start:decl_start].strip().startswith("//"):
                break
            decl_start = previous_start
        return text[decl_start : end + 1]
    return ""


def _find_open_brace(text: str, start: int) -> int:
    i, n = start, len(text)
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
        if char == "'":
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == ";":
            return -1
        if char == "{":
            return i
        i += 1
    return -1


def _match_brace(text: str, open_at: int) -> int:
    depth = 0
    i, n = open_at, len(text)
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
        if char == "'":
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# Statement reduction
# ---------------------------------------------------------------------------
def split_control(token: str) -> list[str]:
    """Split ``if (c) stmt`` into ``if (c)`` and ``stmt``.

    Hex-Rays drops the braces around single-statement branches, so a whole
    ``if``/``else`` pair arrives as two flat statements and the ``else`` ends up
    with no condition attached.  Splitting the head off lets :func:`tree` rebuild
    the branch and lets the reducer pair the arms.
    """
    out: list[str] = []
    rest = token.strip()
    while rest:
        match = CONTROL_PREFIX.match(rest)
        if not match:
            out.append(rest)
            return out
        keyword = match.group("kw")
        cursor = match.end()
        tail = rest[cursor:]
        stripped = tail.lstrip()
        if stripped.startswith("("):
            start = cursor + (len(tail) - len(stripped))
            depth = 0
            index = start
            while index < len(rest):
                if rest[index] == "(":
                    depth += 1
                elif rest[index] == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            head = rest[:index].strip()
            rest = rest[index:].strip()
        else:
            head = keyword
            rest = stripped
        out.append(head)
    return out


def normalise_dispatch(text: str) -> str:
    """Collapse a virtual-dispatch idiom to ``recv->slot(args)``.

    The raw form spans four lines and repeats the receiver twice, so it reads as
    a decompiler artefact rather than evidence.  The rewrite is mechanical: the
    slot name is the managed method name and the receiver is unchanged.
    """
    compact = " ".join(text.split())
    if "->klass->vtable." not in compact:
        return compact
    rewritten = _VTABLE_CAST.sub("", compact)
    head = _VTABLE_HEAD.match(rewritten)
    if head:
        rewritten = f"{head.group(1)}->{head.group(2)}(" + rewritten[head.end() :]
        rewritten = _VTABLE_TAIL.sub(")", rewritten)
    return rewritten


def emit_tokens(token: str, out: list[str]) -> None:
    """Append ``token`` split into a leading label, a control head and a body.

    A label does not end with ``;``, so it arrives glued to the statement it
    introduces.  Left that way the statement is carried by a label node, loses
    its trailing ``;`` and is never scored.
    """
    if not token:
        return
    labelled = LABEL_PREFIX.match(token)
    if labelled and labelled.group("rest"):
        out.append(labelled.group("label"))
        token = labelled.group("rest")
    out.extend(split_control(token))


def statements(text: str | list[str]) -> list[str]:
    """Split decompiled C into statements; ``{``/``}`` become their own tokens."""
    if isinstance(text, list):
        text = "\n".join(text)
    out: list[str] = []
    buf: list[str] = []
    paren = 0
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if char == '"':
            buf.append(char)
            i += 1
            while i < n:
                buf.append(text[i])
                if text[i] == "\\":
                    i += 1
                    if i < n:
                        buf.append(text[i])
                elif text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if char == "(":
            paren += 1
        elif char == ")":
            paren -= 1
        if paren == 0 and char in "{};":
            emit_tokens("".join(buf).strip(), out)
            if char in "{}":
                out.append(char)
            buf = []
            i += 1
            continue
        buf.append(char)
        i += 1
    emit_tokens("".join(buf).strip(), out)
    return out


class Node:
    __slots__ = ("text", "children", "kind")

    def __init__(self, text: str = "", kind: str = "stmt") -> None:
        self.text = text
        self.children: list["Node"] = []
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Node({self.kind}, {self.text[:40]!r}, {len(self.children)})"


def tree(tokens: list[str]) -> list[Node]:
    root: list[Node] = []
    stack: list[list[Node]] = [root]
    pending = ""
    for token in tokens:
        if token == "{":
            node = Node(pending, "block")
            pending = ""
            stack[-1].append(node)
            stack.append(node.children)
            continue
        if token == "}":
            # The statement right before a closing brace has not been flushed
            # yet; dropping it loses the last line of every block.
            if pending:
                stack[-1].append(Node(pending))
                pending = ""
            if len(stack) > 1:
                stack.pop()
            continue
        if LABEL.match(token):
            stack[-1].append(Node(token, "label"))
            pending = ""
            continue
        if pending and CONTROL_HEAD.match(pending):
            # A brace-less branch owns exactly the next token.
            node = Node(pending, "block")
            node.children.append(Node(token))
            stack[-1].append(node)
            pending = ""
            continue
        if pending:
            stack[-1].append(Node(pending))
        pending = token
    if pending:
        stack[-1].append(Node(pending))
    return root


class Reducer:
    """Behaviour-preserving view of one decompiled function."""

    def __init__(
        self,
        plumbing: Iterable[str] | None = None,
        framework_prefixes: Iterable[str] | None = None,
        symbol_roles: Dict[str, str] | None = None,
    ) -> None:
        self.plumbing = set(plumbing or ()) | set(HelperTable().plumbing())
        self.framework_prefixes = tuple(framework_prefixes or ())
        # ``mangled type -> "framework" | "business"`` decided by the index.
        self.symbol_roles = symbol_roles or {}

    def is_framework(self, mangled_type: str) -> bool:
        role = self.symbol_roles.get(mangled_type)
        if role:
            return role == "framework"
        return any(mangled_type.startswith(prefix) for prefix in self.framework_prefixes)

    def is_seed(self, text: str) -> bool:
        slot = VTABLE_SLOT.search(text)
        if slot and slot.group(2) not in VTABLE_PLUMBING:
            # A virtual call is behaviour even when the method lives in a
            # framework assembly: it mutates an object the member holds.
            return True
        calls = re.findall(r"\b([A-Za-z_]\w*)\s*\(", text)
        if calls and all(call in self.plumbing for call in calls):
            return False
        # Struct shuffling between temporaries carries no behaviour.  A call is
        # kept even on a temporary, because ``FromSeconds(0.6)`` stores its
        # result exactly that way.
        if not calls and TEMP_FIELD.search(text):
            return False
        if PLUMBING_SHAPE.search(text):
            business = [
                name
                for cls, name in BUSINESS_CALL.findall(text)
                if not self.is_framework(cls)
            ]
            if not business:
                return False
        if LITERAL.search(text):
            return True
        if FIELD.search(text):
            return True
        return any(not self.is_framework(cls) for cls, _ in BUSINESS_CALL.findall(text))

    def relevant(self, node: Node) -> bool:
        if node.kind == "stmt":
            return self.is_seed(node.text)
        return any(self.relevant(child) for child in node.children)

    def reduce(self, text: str | list[str]) -> list[str]:
        out: list[str] = []
        self._emit(tree(statements(text)), out, 0, "  ")
        return out

    def statements_only(self, text: str | list[str]) -> list[str]:
        out: list[str] = []
        for line in self.reduce(text):
            stripped = line.strip()
            if stripped and stripped not in "{}" and not stripped.startswith("//"):
                out.append(stripped)
        return out

    # -- internals --------------------------------------------------------
    def _emit(self, nodes: list[Node], out: list[str], depth: int, indent: str) -> None:
        pending_labels: list[str] = []
        index, total = 0, len(nodes)
        while index < total:
            node = nodes[index]
            index += 1
            if node.kind == "label":
                # A label only makes sense if the statement it introduces is
                # kept; emitting it unconditionally leaves dangling ``LABEL_37:``.
                pending_labels.append(node.text)
                continue
            before = len(out)
            if node.kind == "block" and IF_HEAD.match(node.text.strip()):
                chain = [node]
                while (
                    index < total
                    and nodes[index].kind == "block"
                    and ELSE_HEAD.match(nodes[index].text.strip())
                ):
                    chain.append(nodes[index])
                    index += 1
                self._emit_chain(chain, out, depth, indent)
            elif node.kind == "block" and ELSE_HEAD.match(node.text.strip()):
                # An ``else`` whose ``if`` lives outside this list.  Emit the body
                # without the keyword so the view never shows a branch with no
                # condition, which reads as a decompiler artefact.
                self._emit_chain([node], out, depth, indent, drop_head=True)
            elif node.kind == "stmt":
                if self.is_seed(node.text):
                    out.append(indent * depth + normalise_dispatch(node.text) + ";")
            else:
                # Render the body first: whether the head survives depends on
                # whether anything inside did.  Filtering the children up front
                # would split an ``if``/``else`` pair and orphan the ``else``.
                inner: list[str] = []
                self._emit(node.children, inner, depth + 1, indent)
                if inner:
                    head = " ".join(node.text.split())
                    if head:
                        out.append(indent * depth + head)
                    out.append(indent * depth + "{")
                    out.extend(inner)
                    out.append(indent * depth + "}")
            if len(out) > before:
                for offset, label in enumerate(pending_labels):
                    out.insert(before + offset, indent * depth + label)
                pending_labels = []

    def _emit_chain(
        self,
        chain: list[Node],
        out: list[str],
        depth: int,
        indent: str,
        drop_head: bool = False,
    ) -> None:
        """Emit an ``if`` / ``else if`` / ``else`` chain as one unit.

        Emitting the arms independently leaves a dangling ``else`` whenever the
        ``if`` arm holds no anchors.  The chain is kept whole as soon as any arm
        is relevant, and an arm without anchors is marked rather than silently
        blanked -- a missing branch is itself evidence about the native shape.
        """
        rendered: list[tuple[str, list[str]]] = []
        for node in chain:
            inner: list[str] = []
            self._emit(node.children, inner, depth + 1, indent)
            rendered.append((" ".join(node.text.split()), inner))
        # A guard whose body holds no anchors still matters when the condition
        # reads a field: it is the native null check, and dropping it hides a
        # branch the C# has to keep.
        if not any(inner for _, inner in rendered) and not any(
            self.is_seed(head) for head, _ in rendered if head
        ):
            return
        for position, (head, inner) in enumerate(rendered):
            if drop_head and position == 0 and head.startswith("else"):
                head = ""
            if head:
                out.append(indent * depth + head)
            out.append(indent * depth + "{")
            if inner:
                out.extend(inner)
            else:
                out.append(indent * (depth + 1) + "// no anchors in this branch")
            out.append(indent * depth + "}")


def strip_class_init_guard(lines: list[str]) -> list[str]:
    """Drop the leading ``if ((byte_X & 1) == 0) { ...init... }`` block.

    The guard exists only in compiled IL2CPP output; leaving it in makes the
    evidence look like it has a branch the C# must reproduce.
    """
    out: list[str] = []
    awaiting = False
    skip = 0
    for line in lines:
        if not out and CLASS_INIT_GUARD.search(line):
            awaiting = True
            continue
        if awaiting:
            if "{" in line:
                skip = 1
                awaiting = False
            continue
        if skip:
            skip += line.count("{") - line.count("}")
            continue
        stripped = line.strip()
        if not stripped or DECL_ONLY.match(stripped):
            continue
        out.append(line)
    return out


def function_body(text: str) -> str:
    """The part of ``text`` between the outermost braces."""
    start = text.find("{")
    if start == -1:
        return text
    end = _match_brace(text, start)
    if end == -1:
        return text[start + 1 :]
    return text[start + 1 : end]


def signature_of(text: str) -> str:
    """The declaration part of a function text (everything before the body)."""
    start = text.find("{")
    return (text[:start] if start != -1 else text).strip()
