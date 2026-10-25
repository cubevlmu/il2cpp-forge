"""Command line interface.

Every stage is a subcommand, every subcommand prints a short human summary and,
with ``--json``, a machine record.  That split is deliberate: an agent drives the
tool by parsing JSON, a person reads the summary, and neither needs a server.

Typical session::

    il2cpp-forge init
    il2cpp-forge detect --input game.apk
    il2cpp-forge dump --exe tools/Il2CppDumper/Il2CppDumper.exe
    il2cpp-forge index --native-dir native/split-c
    il2cpp-forge queue --src work/src --csv work/open.csv
    il2cpp-forge cards --src work/src --limit 20 --out work/cards --markdown
    il2cpp-forge apply --patch work/patch.json --src work/src --dry-run
    il2cpp-forge apply --patch work/patch.json --src work/src
    il2cpp-forge verify --patch work/patch.json --src work/src
    il2cpp-forge build --src work/src --refs dump/DummyDll
    il2cpp-forge tally --src work/src --out work/STATUS.md
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import __version__
from .build import diagnostics as diaglib
from .build import project as projectlib
from .config import Config
from .csharp import body as bodylib
from .dump import dumpscript
from .index import builder as indexlib
from .native import anchors as anchorlib
from .native import helpers as helperlib
from .native.decompiled import FunctionIndex
from .report import tally as tallylib
from .restore import apply as applylib
from .restore import cards as cardlib
from .restore import queue as queuelib
from .restore import verify as verifylib
from .unity import apk as apklib
from .unity import metadata as metalib
from .util import read_json, write_json, write_jsonl


# ---------------------------------------------------------------------------
# Runtime context
# ---------------------------------------------------------------------------
class Runtime:
    """Lazy, per-invocation state shared by the subcommands."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.index_dir = config.path_of("index.dir") or (config.root / "index")
        self._methods: Optional[List[dict]] = None
        self._types: Optional[Dict[str, dict]] = None
        self._symbols: Optional[Dict[str, dict]] = None
        self._functions: Optional[FunctionIndex] = None
        self._helpers: Optional[helperlib.HelperTable] = None

    # -- data -------------------------------------------------------------
    @property
    def methods(self) -> List[dict]:
        if self._methods is None:
            self._methods = indexlib.load_methods(self.index_dir)
        return self._methods

    @property
    def types(self) -> Dict[str, dict]:
        if self._types is None:
            self._types = {}
            path = self.index_dir / "types.jsonl"
            if path.is_file():
                with path.open("r", encoding="utf-8-sig") as handle:
                    for line in handle:
                        line = line.strip()
                        if line:
                            row = json.loads(line)
                            self._types[row.get("full_name", "")] = row
        return self._types

    @property
    def symbols(self) -> Dict[str, dict]:
        if self._symbols is None:
            self._symbols = indexlib.load_symbols(self.index_dir)
        return self._symbols

    def function_index(self, native_dir: Optional[Path] = None) -> Optional[FunctionIndex]:
        native_dir = native_dir or self.config.path_of("native.c_dir")
        if self._functions is None and native_dir and Path(native_dir).is_dir():
            self._functions = FunctionIndex.build(
                native_dir, cache_file=self.index_dir / "native_functions.tsv"
            )
        return self._functions

    def helpers(self, native_dir: Optional[Path] = None) -> helperlib.HelperTable:
        if self._helpers is not None:
            return self._helpers
        table = helperlib.HelperTable()
        configured = self.config.path_of("native.helper_file")
        if configured and configured.is_file():
            table = table.merge(helperlib.HelperTable.load(configured))
        index = self.function_index(native_dir)
        if index is not None and not table.all_symbols():
            table = table.merge(helperlib.learn_from_corpus(index.sample(1500)))
        self._helpers = table
        return table

    def card_context(self, native_dir: Optional[Path] = None) -> cardlib.CardContext:
        native_dir = native_dir or self.config.path_of("native.c_dir")
        return cardlib.CardContext(
            function_index=self.function_index(native_dir),
            script=scriptmap_load(self.config),
            helpers=self.helpers(native_dir),
            symbol_roles=indexlib.symbol_roles(self.symbols),
            framework_prefixes=self.config.framework_prefixes
            or list(read_json(self.index_dir / "summary.json", {}).get("framework_prefixes", [])),
            types=self.types,
            policy=policy_of(self.config),
            native_root=Path(native_dir) if native_dir else None,
        )


def policy_of(config: Config) -> bodylib.BodyPolicy:
    return bodylib.BodyPolicy(banner=config.evidence_banner)


def scriptmap_load(config: Config):
    from .index import script as scriptmap

    path = config.path_of("dump.script_json")
    return scriptmap.load(path) if path else scriptmap.ScriptMap()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def _discover_outputs(directory: Path) -> dict:
    """Locate dumper artefacts inside an output directory."""
    found = {
        "dump_cs": "",
        "script_json": "",
        "stringliteral_json": "",
        "dummy_dll_dir": "",
        "skeleton_src": "",
    }
    if not directory.is_dir():
        return found
    for candidate in directory.rglob("dump.cs"):
        found["dump_cs"] = str(candidate)
        break
    for name, key in (("script.json", "script_json"), ("stringliteral.json", "stringliteral_json")):
        for candidate in directory.rglob(name):
            found[key] = str(candidate)
            break
    for candidate in directory.rglob("DummyDll"):
        if candidate.is_dir():
            found["dummy_dll_dir"] = str(candidate)
            break
    for name in ("cs", "diffable-cs", "cpp2il_out"):
        for candidate in directory.rglob(name):
            if candidate.is_dir() and any(candidate.rglob("*.cs")):
                found["skeleton_src"] = str(candidate)
                break
    return found


def _enrich(entries: List[dict], methods: List[dict]) -> List[dict]:
    """Attach native name/file to patch entries that do not carry one.

    ``(name, param count)`` alone is ambiguous: hundreds of types declare
    ``Show``/``Hide``/``Update``.  Picking the first candidate silently points a
    card at another type's function, which reads as real evidence.  When the
    owner cannot narrow it to one, the entry is left without a native link and
    marked, so the gap is visible instead of wrong.
    """
    lookup: Dict[tuple, List[dict]] = {}
    for row in methods:
        lookup.setdefault((row.get("name", ""), len(row.get("params", []))), []).append(row)
    for entry in entries:
        if entry.get("native_name"):
            continue
        candidates = lookup.get((entry.get("member", ""), int(entry.get("params", 0))), [])
        if len(candidates) > 1:
            stem = Path(entry.get("file", "")).stem
            narrowed = [
                c for c in candidates if c.get("owner", "").split(".")[-1] == stem
            ]
            if narrowed:
                candidates = narrowed
        if len(candidates) == 1:
            entry.setdefault("native_name", candidates[0].get("native_name", ""))
            entry.setdefault("native_file", candidates[0].get("native_file", ""))
            entry.setdefault("rva", candidates[0].get("rva", 0))
        elif len(candidates) > 1:
            entry.setdefault("native_ambiguous", len(candidates))
    return entries


def _entries_from_queue(queue: queuelib.Queue) -> List[dict]:
    return [entry.as_dict() for entry in queue.open_entries()]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.output or "forge.json")
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 0
    config = Config()
    config.save(target)
    print(f"wrote {target}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    workdir = Path(args.work or (config.root / "work"))
    payload = apklib.stage(args.input, workdir)
    if payload.libil2cpp:
        config.set("input.libil2cpp", _rel_to(payload.libil2cpp, config.root))
    if payload.metadata:
        config.set("input.metadata", _rel_to(payload.metadata, config.root))
    if payload.source.is_file():
        config.set("input.apk", _rel_to(payload.source, config.root))
    if payload.unity_version:
        config.set("project.unity_version", payload.unity_version)
    if not args.no_write and config.path:
        config.save()
    summary = payload.as_dict()
    summary["unity_version"] = payload.unity_version or summary.get("unity_version", "")
    if args.json:
        _print_json(summary)
    else:
        print(f"source      : {payload.source}")
        print(f"libil2cpp   : {payload.libil2cpp or '-'}")
        print(f"metadata    : {payload.metadata or '-'}")
        print(f"abi         : {payload.abi or '-'}")
        print(f"metadata ver: {payload.metadata_version} ({payload.unity_hint})")
        print(f"unity       : {payload.unity_version or 'unknown'}")
        for note in payload.notes:
            print(f"note        : {note}")
    return 0 if payload.complete else 1


def cmd_metadata(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    path = Path(args.metadata) if args.metadata else config.path_of("input.metadata")
    if not path or not Path(path).is_file():
        print("no global-metadata.dat: pass --metadata", file=sys.stderr)
        return 2
    header = metalib.read_header(path)
    payload = header.as_dict()
    if args.literals:
        literals = metalib.string_literals(path, limit=args.literals)
        payload["sample"] = literals[: args.literals]
    if args.json:
        _print_json(payload)
    else:
        for key, value in payload.items():
            if key == "sample":
                continue
            print(f"{key:18}: {value}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    so = Path(args.libil2cpp) if args.libil2cpp else config.path_of("input.libil2cpp")
    metadata = Path(args.metadata) if args.metadata else config.path_of("input.metadata")
    outdir = Path(args.out or (config.root / "dump"))
    tool = (args.tool or config.get("dump.tool", "auto")).lower()
    if tool == "auto":
        tool = "cpp2il" if "cpp2il" in str(args.exe or "").lower() else "il2cppdumper"
    if not args.exe:
        print("no dumper executable: pass --exe", file=sys.stderr)
        return 2
    if tool == "il2cppdumper":
        if not so or not metadata:
            print("il2cppdumper needs --libil2cpp and --metadata", file=sys.stderr)
            return 2
        command = [str(args.exe), str(so), str(metadata), str(outdir)]
    else:
        source = Path(args.input) if args.input else config.path_of("input.apk")
        if not source:
            print("cpp2il needs --input (apk or game folder)", file=sys.stderr)
            return 2
        command = [str(args.exe), "--input", str(source), "--output", str(outdir)]
        if args.game_path:
            command += ["--game-path", str(args.game_path)]
    outdir.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        print(" ".join(command))
        return 0
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    tail = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()[-8:]
    found = _discover_outputs(outdir)
    for key, value in found.items():
        if value:
            config.set(f"dump.{key}", _rel_to(Path(value), config.root))
    if config.path and not args.no_write:
        config.save()
    if args.json:
        _print_json({"command": command, "exit_code": completed.returncode, "outputs": found})
    else:
        print(f"exit {completed.returncode}")
        for line in tail:
            print(f"  {line}")
        for key, value in found.items():
            print(f"{key:20}: {value or '-'}")
    return completed.returncode


def cmd_index(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    summary = indexlib.build(
        config,
        dump_cs=args.dump_cs,
        native_dir=args.native_dir,
        script_json=args.script,
        scope=args.scope,
        index_dir=args.index_dir,
        image_base=int(args.image_base, 16) if args.image_base else None,
    )
    # Persist where the index went: every later command reads it, and a
    # relative path resolved against the wrong root is a silent empty index.
    if config.path and not args.no_write:
        resolved = Path(args.index_dir) if args.index_dir else (config.root / "index")
        config.set("index.dir", _rel_to(resolved, config.root))
        if args.write_config:
            config.set("native.framework_prefixes", summary.framework_prefixes)
        config.save()
    payload = summary.as_dict()
    if args.json:
        _print_json(payload)
    else:
        print(f"dump.cs        : {summary.dump_cs}")
        print(f"types          : {summary.types}")
        print(f"methods        : {summary.methods}")
        print(f"target methods : {summary.target_methods}")
        print(f"native join    : {summary.methods_with_native} "
              f"(script {summary.joined_by_script}, name {summary.joined_by_name}, "
              f"address {summary.joined_by_address})")
        print(f"image base     : {payload['image_base_hex']}")
        print(f"native funcs   : {summary.native_functions}")
        print(f"literals       : {summary.literals}")
        print(f"index dir      : {args.index_dir or (config.root / 'index')}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    runtime = Runtime(config)
    src = Path(args.src) if args.src else config.path_of("source.src_dir")
    if not src:
        print("no source tree: pass --src", file=sys.stderr)
        return 2
    queue = queuelib.scan(src, runtime.methods, policy_of(config), only=args.kind)
    if args.csv:
        queuelib.write_csv(queue, args.csv)
    if args.json_out:
        queuelib.write_json(queue, args.json_out)
    payload = {"files": queue.files, "members": queue.members, "counts": queue.counts,
               "open": len(queue.open_entries())}
    if args.json:
        _print_json(payload)
    else:
        print(f"files   : {queue.files}")
        print(f"members : {queue.members}")
        for kind, value in queue.counts.items():
            print(f"  {kind:14}: {value}")
        print(f"open    : {len(queue.open_entries())}")
        if args.csv:
            print(f"csv     : {args.csv}")
    if args.limit:
        for entry in queuelib.rank(queue)[: args.limit]:
            print(f"  {entry.kind:13} {entry.file}::{entry.member}/{entry.params}")
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    runtime = Runtime(config)
    src = Path(args.src) if args.src else config.path_of("source.src_dir")
    if not src:
        print("no source tree: pass --src", file=sys.stderr)
        return 2
    context = runtime.card_context()
    instructions = ""
    if args.instructions_file:
        instructions = Path(args.instructions_file).read_text(encoding="utf-8")
    queue = queuelib.scan(src, runtime.methods, policy_of(config), only=args.kind)
    entries = queuelib.rank(queue)
    if args.match:
        needle = args.match.lower()
        entries = [
            e for e in entries
            if needle in e.member.lower() or needle in e.owner.lower() or needle in e.file.lower()
        ]
    if args.id:
        entries = [e for e in entries if args.id in e.key or args.id == e.member]
    if args.limit:
        entries = entries[: args.limit]
    outdir = Path(args.out or (config.root / "cards"))
    written = []
    for entry in entries:
        card = cardlib.build_card(entry, context, src_dir=src, instructions=instructions)
        written.append(cardlib.write_card(card, outdir, markdown=args.markdown))
    if args.json:
        _print_json({"cards": [str(p) for p in written]})
    else:
        print(f"cards written: {len(written)} -> {outdir}")
        for path in written[:10]:
            print(f"  {path.name}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    src = Path(args.src) if args.src else config.path_of("source.src_dir")
    if not src:
        print("no source tree: pass --src", file=sys.stderr)
        return 2
    result = applylib.apply_patch(
        args.patch,
        src,
        backup_dir=args.backup,
        dry_run=args.dry_run,
        allow_restored=args.allow_restored,
        policy=policy_of(config),
    )
    if args.json:
        _print_json(result.as_dict())
    else:
        print(f"{'[dry] ' if args.dry_run else ''}written={result.written} skipped={result.skipped}")
        for problem in result.problems[:20]:
            print(f"  {problem}")
        if result.snapshot_path:
            print(f"evidence snapshot -> {result.snapshot_path}")
    return 1 if result.problems and args.strict else 0


def cmd_verify(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    runtime = Runtime(config)
    src = Path(args.src) if args.src else config.path_of("source.src_dir")
    if not src:
        print("no source tree: pass --src", file=sys.stderr)
        return 2
    if args.patch:
        entries = json.loads(Path(args.patch).read_text(encoding="utf-8-sig"))
        if isinstance(entries, dict):
            entries = entries.get("entries", [])
    elif args.queue:
        payload = json.loads(Path(args.queue).read_text(encoding="utf-8-sig"))
        entries = payload.get("entries", payload if isinstance(payload, list) else [])
        entries = [e for e in entries if e.get("kind") != "restored"]
    else:
        queue = queuelib.scan(src, runtime.methods, policy_of(config))
        entries = _entries_from_queue(queue)
    entries = _enrich(entries, runtime.methods)
    snapshot = verifylib.load_snapshot(args.snapshot) if args.snapshot else {}
    verdicts = verifylib.verify(
        entries, src, runtime.card_context(), min_coverage=args.min_coverage, snapshot=snapshot
    )
    payload = verifylib.summary(verdicts)
    if args.report:
        write_json(args.report, payload)
    if args.json:
        _print_json(payload)
    else:
        print(f"checked={payload['checked']} ok={payload['ok']} failed={payload['failed']}")
        for issue, count in payload["by_issue"].items():
            print(f"  {issue:16}: {count}")
        for failure in payload["failures"][:15]:
            print(f"  {failure['file']}::{failure['member']}/{failure['params']} "
                  f"-> {', '.join(failure['issues'])}")
    return 0 if payload["failed"] == 0 else 1


def cmd_tally(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    runtime = Runtime(config)
    src = Path(args.src) if args.src else config.path_of("source.src_dir")
    if not src:
        print("no source tree: pass --src", file=sys.stderr)
        return 2
    queue = queuelib.scan(src, runtime.methods, policy_of(config))
    index_summary = read_json(runtime.index_dir / "summary.json", {}) or {}
    build_summary = read_json(args.build_json, None) if args.build_json else None
    verify_summary = read_json(args.verify_json, None) if args.verify_json else None
    text = tallylib.queue_markdown(
        queue,
        title=args.title or f"{config.get('project.name') or 'Reconstruction'} status",
        index_summary=index_summary,
        build_summary=build_summary,
        verify_summary=verify_summary,
    )
    if args.out:
        tallylib.write(args.out, text)
        print(f"report -> {args.out}")
    else:
        print(text)
    if args.json:
        _print_json({"files": queue.files, "members": queue.members, "counts": queue.counts})
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    src = Path(args.src) if args.src else config.path_of("source.src_dir")
    if not src:
        print("no source tree: pass --src", file=sys.stderr)
        return 2
    refs = Path(args.refs) if args.refs else config.path_of("dump.dummy_dll_dir")
    csproj = Path(args.csproj) if args.csproj else (config.root / "build" / "Recovery.csproj")
    exclude = args.exclude or config.get("build.exclude_references", []) or [
        Path(name).stem for name in config.target_assemblies
    ]
    result = projectlib.compile_tree(
        src,
        refs,
        csproj,
        exclude=exclude,
        framework=args.framework or config.get("build.target_framework", "netstandard2.1"),
        langversion=config.get("build.lang_version", "latest"),
        dotnet=args.dotnet or config.get("build.dotnet", "dotnet"),
        timeout=args.timeout,
        residue=not args.no_residue,
    )
    payload = result.as_dict()
    payload["residue"] = diaglib.scan_residue(src)
    if args.report:
        write_json(args.report, payload)
    if args.json:
        _print_json(payload)
    else:
        print(f"exit           : {result.exit_code}")
        print(f"errors         : {payload['errors']}")
        print(f"syntax errors  : {payload['syntax_errors']}")
        print(f"warnings       : {payload['warnings']}")
        codes = payload.get("codes") or {}
        if codes:
            print("top codes      : " + ", ".join(f"{k}={v}" for k, v in list(codes.items())[:8]))
        residue = {k: v for k, v in payload["residue"].items() if v}
        print("residue        : " + (", ".join(f"{k}={v}" for k, v in residue.items()) or "none"))
    passed, reasons = diaglib.gate(
        result.report,
        max_syntax_errors=config.get("gates.max_syntax_errors", 0),
        residue=payload["residue"] if config.get("gates.check_residue", True) else None,
        max_residue=config.get("gates.max_residue", 0),
    )
    if not args.json:
        print("gate           : " + ("pass" if passed else "fail — " + "; ".join(reasons)))
    return 0 if passed or args.no_gate else 1


def cmd_helpers(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    native_dir = Path(args.native_dir) if args.native_dir else config.path_of("native.c_dir")
    table = helperlib.HelperTable()
    configured = config.path_of("native.helper_file")
    if configured and configured.is_file():
        table = table.merge(helperlib.HelperTable.load(configured))
    if native_dir and Path(native_dir).is_dir():
        index = FunctionIndex.build(
            native_dir, cache_file=(config.path_of("index.dir") or (config.root / "index"))
            / "native_functions.tsv"
        )
        if args.symbols:
            symbols = helperlib.parse_symbol_dump(Path(args.symbols).read_text(encoding="utf-8"))
            table = table.merge(helperlib.learn_from_symbols(symbols, index.by_address))
        if args.learn:
            table = table.merge(helperlib.learn_from_corpus(index.sample(args.sample)))
    payload = table.to_json()
    if args.out:
        helperlib.save(table, args.out)
        print(f"helpers -> {args.out}")
    if args.json:
        _print_json(payload)
    else:
        for role, symbols in payload.items():
            if role.startswith("_"):
                continue
            print(f"{role:18}: {', '.join(symbols) if symbols else '-'}")
        print(f"source            : {table.source}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    checks = []
    checks.append(("python", f"{sys.version.split()[0]} ({sys.executable})", True))
    dotnet = shutil.which(config.get("build.dotnet", "dotnet") or "dotnet")
    checks.append(("dotnet", dotnet or "not found", bool(dotnet)))
    for label, dotted in (
        ("libil2cpp", "input.libil2cpp"),
        ("metadata", "input.metadata"),
        ("dump.cs", "dump.dump_cs"),
        ("script.json", "dump.script_json"),
        ("native c dir", "native.c_dir"),
        ("source tree", "source.src_dir"),
        ("reference dlls", "dump.dummy_dll_dir"),
    ):
        path = config.path_of(dotted)
        checks.append((label, str(path) if path else "-", bool(path and Path(path).exists())))
    index_dir = config.path_of("index.dir") or (config.root / "index")
    methods = index_dir / "methods.jsonl"
    checks.append(("index", str(index_dir), methods.is_file()))
    if args.json:
        _print_json({"config": str(config.path or ""), "checks": checks})
    else:
        print(f"config: {config.path or '(defaults)'}")
        for label, value, ok in checks:
            print(f"  [{'ok' if ok else '--'}] {label:14} {value}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def runtime_config(args: argparse.Namespace) -> Config:
    if getattr(args, "config", None):
        return Config.load(args.config)
    return Config.discover(getattr(args, "cwd", None) or os.getcwd())


def _rel_to(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(Path(path).resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="il2cpp-forge",
        description="AI-native, evidence-gated reconstruction of Unity IL2CPP assemblies.",
    )
    parser.add_argument("--version", action="version", version=f"il2cpp-forge {__version__}")
    parser.add_argument("--config", help="path to forge.json (default: search upwards)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter forge.json")
    p.add_argument("--output", default="forge.json")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("detect", help="locate libil2cpp.so and global-metadata.dat")
    p.add_argument("--input", required=True, help="apk / xapk / split-apk dir / folder")
    p.add_argument("--work", help="staging directory")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("metadata", help="read global-metadata.dat header and literals")
    p.add_argument("--metadata")
    p.add_argument("--literals", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_metadata)

    p = sub.add_parser("dump", help="run Il2CppDumper or Cpp2IL")
    p.add_argument("--exe", required=True)
    p.add_argument("--tool", default="auto", choices=["auto", "il2cppdumper", "cpp2il"])
    p.add_argument("--libil2cpp")
    p.add_argument("--metadata")
    p.add_argument("--input", help="game apk/folder for cpp2il")
    p.add_argument("--game-path", dest="game_path")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("index", help="build the method/type/native index")
    p.add_argument("--dump-cs")
    p.add_argument("--native-dir", dest="native_dir")
    p.add_argument("--script")
    p.add_argument("--scope", default="target", choices=["target", "all"])
    p.add_argument("--index-dir", dest="index_dir")
    p.add_argument("--image-base", dest="image_base")
    p.add_argument("--write-config", action="store_true")
    p.add_argument("--no-write", action="store_true", help="do not touch forge.json")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("queue", help="classify every member in a tree")
    p.add_argument("--src")
    p.add_argument("--kind", nargs="*", help="only these kinds")
    p.add_argument("--csv")
    p.add_argument("--json-out", dest="json_out")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("cards", help="emit per-member evidence cards")
    p.add_argument("--src")
    p.add_argument("--out")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--kind", nargs="*")
    p.add_argument("--match")
    p.add_argument("--id")
    p.add_argument("--markdown", action="store_true")
    p.add_argument("--instructions-file", dest="instructions_file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cards)

    p = sub.add_parser("apply", help="write bodies from a JSON patch")
    p.add_argument("--patch", required=True)
    p.add_argument("--src")
    p.add_argument("--backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-restored", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("verify", help="judge written bodies against their evidence")
    p.add_argument("--patch")
    p.add_argument("--queue")
    p.add_argument("--src")
    p.add_argument("--snapshot")
    p.add_argument("--min-coverage", type=float, default=0.6)
    p.add_argument("--report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("tally", help="write the status report")
    p.add_argument("--src")
    p.add_argument("--out")
    p.add_argument("--title")
    p.add_argument("--build-json", dest="build_json")
    p.add_argument("--verify-json", dest="verify_json")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tally)

    p = sub.add_parser("build", help="compile the tree as a diagnostic")
    p.add_argument("--src")
    p.add_argument("--refs")
    p.add_argument("--csproj")
    p.add_argument("--framework")
    p.add_argument("--dotnet")
    p.add_argument("--timeout", type=int, default=2400)
    p.add_argument("--exclude", nargs="*")
    p.add_argument("--no-residue", action="store_true")
    p.add_argument("--no-gate", action="store_true")
    p.add_argument("--report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("helpers", help="discover IL2CPP runtime helpers")
    p.add_argument("--native-dir", dest="native_dir")
    p.add_argument("--symbols", help="nm -D output for libil2cpp.so")
    p.add_argument("--learn", action="store_true", help="infer from decompiled corpus")
    p.add_argument("--sample", type=int, default=1500)
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_helpers)

    p = sub.add_parser("doctor", help="check the environment and config")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
