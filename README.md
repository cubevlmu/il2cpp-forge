# il2cpp-forge

**English** | [简体中文](README.zh-CN.md)

**Evidence-driven C# reconstruction tooling for Unity IL2CPP projects.**

`il2cpp-forge` connects IL2CPP metadata, decompiled native code, and a reconstructed C# source tree. It identifies unfinished methods, packages bounded evidence for a human or AI assistant, applies proposed method bodies, and reports structural and compilation diagnostics.

It is a **local, command-line toolkit**, not a one-click IL2CPP-to-C# decompiler. It does not call a language model, operate IDA/Ghidra, or reconstruct Unity scenes and assets on its own. Reconstructed code is a hypothesis supported by evidence, **not the original source code**.

## Why use it?

- **Evidence before generation.** Match managed methods to native implementations using Il2CppDumper output, `script.json`, names, and address information.
- **Small, inspectable inputs.** Produce one JSON evidence card per unfinished C# member, containing its signature, type context, reduced native behavior, resolved string literals, and review anchors.
- **No silent completion.** Distinguish partial bodies, stubs, remaining recovery markers, and suspiciously empty methods instead of trusting a cleared comment block.
- **Controlled writes.** Preview a JSON patch, preserve previous evidence, and avoid overwriting completed members by default.
- **Reproducible diagnostics.** Export indexes and reports as JSON/JSONL; use `.NET` compilation for syntax and type feedback.
- **No required Python runtime packages.** The CLI uses the Python standard library. External reverse-engineering tools and the .NET SDK are optional, depending on the stage.

## Requirements

- Python **3.10+**.
- [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) for the primary metadata-dump workflow. [Cpp2IL](https://github.com/SamboyCoding/Cpp2IL) is an alternative input tool, but its output and command-line options vary by version.
- [Ghidra](https://github.com/NationalSecurityAgency/ghidra) or IDA/Hex-Rays to **export native C-like decompilation separately**. Current native reduction rules are primarily tailored to Hex-Rays output.
- A .NET SDK with the configured target framework available, for `build` only.
- A pre-existing C# skeleton/source tree to scan and patch. The CLI **does not automatically generate a full C# source tree** from `DummyDll`.

Work only with builds and assets you are authorized to analyze.

## Install

From a checkout or the extracted release archive:

```bash
python -m pip install -e .
il2cpp-forge --version
```

Alternatively, run directly from the repository without installing:

```bash
python -m forge.cli --help
```

The short command `forge` and the full command `il2cpp-forge` invoke the same CLI after installation.

## Quick start

The following workflow assumes the external tools and a C# skeleton are available. **Review `forge.json` after initialization**, especially `native.target_assemblies` and `source.src_dir`.

```bash
# 1. Create the local project configuration.
il2cpp-forge init

# 2. Locate libil2cpp.so and global-metadata.dat in an APK/XAPK/split package.
il2cpp-forge detect --input game.apk

# 3. Dump metadata and managed signatures with Il2CppDumper.
il2cpp-forge dump --tool il2cppdumper --exe /path/to/Il2CppDumper

# 4. Export native pseudo-C with IDA/Ghidra separately, then build the index.
il2cpp-forge index --native-dir native/split-c

# 5. Scan the existing C# source tree for unresolved members.
il2cpp-forge queue --src work/src --csv work/open.csv

# 6. Produce bounded evidence cards for external review or model input.
il2cpp-forge cards --src work/src --limit 20 --out work/cards --markdown

# 7. Prepare a JSON patch from the proposed method bodies, and inspect it.
il2cpp-forge apply --patch work/patch.json --src work/src --dry-run

# 8. Apply the patch while saving file backups and the original evidence.
il2cpp-forge apply --patch work/patch.json --src work/src --backup work/backup

# 9. Validate against the evidence snapshot and compile the source tree.
il2cpp-forge verify --patch work/patch.json --src work/src \
  --snapshot work/patch.evidence.json --json
il2cpp-forge build --src work/src --refs dump/DummyDll --json

# 10. Summarize what remains.
il2cpp-forge tally --src work/src --out work/STATUS.md
```

`detect` and `dump` may update an existing `forge.json` with discovered paths. `index` writes its outputs to the configured index directory. `apply` creates the `*.evidence.json` snapshot **when it writes accepted bodies**. Run `doctor` to inspect your configured paths before processing a large project.

## Pipeline and commands

| Command | Purpose | Main output |
| --- | --- | --- |
| `init` | Create `forge.json` | Local configuration |
| `detect` | Locate IL2CPP binary and metadata in APK/XAPK/split inputs | Extracted payload paths and metadata information |
| `metadata` | Inspect metadata header and sample string literals | Console or JSON output |
| `dump` | Invoke Il2CppDumper or Cpp2IL | Tool-dependent dump artifacts |
| `index` | Join managed methods, native symbols, and address information | `methods.jsonl`, `types.jsonl`, `symbols.json`, and summaries |
| `helpers` | Identify common IL2CPP runtime helpers by symbols or corpus heuristics | `helpers.json` |
| `queue` | Classify unresolved C# members | Counts, CSV, or JSON |
| `cards` | Export per-member evidence cards | JSON and optional Markdown |
| `apply` | Preview or insert method bodies | Updated C# and evidence snapshots |
| `verify` | Check candidate bodies for residue, markers, and evidence coverage | Per-member findings |
| `build` | Generate a diagnostic C# project and invoke `dotnet build` | Compiler diagnostics and configurable gate |
| `tally` | Summarize queue and optional validation results | Markdown status report |
| `doctor` | Check configured inputs and external tools | Environment summary |

For full flags, use `il2cpp-forge <command> --help`.

## Configuration

`forge.json` stores project-specific paths. Relative paths resolve against the directory containing that file, not necessarily the current shell directory.

```json
{
  "project": { "name": "MyGame", "evidence_banner": "UNRESTORED EVIDENCE" },
  "input": { "apk": "", "libil2cpp": "", "metadata": "" },
  "dump": {
    "dump_cs": "dump/dump.cs",
    "script_json": "dump/script.json",
    "dummy_dll_dir": "dump/DummyDll",
    "skeleton_src": ""
  },
  "native": {
    "c_dir": "native/split-c",
    "helper_file": "index/helpers.json",
    "target_assemblies": ["Assembly-CSharp.dll", "MyGame.dll"]
  },
  "source": { "src_dir": "work/src" },
  "index": { "dir": "index" },
  "build": { "dotnet": "dotnet", "target_framework": "netstandard2.1" },
  "gates": { "min_coverage": 0.6, "max_syntax_errors": 0, "check_residue": true, "max_residue": 0 }
}
```

See [`examples/forge.example.json`](examples/forge.example.json) for a fuller example. Include all application-specific assemblies in `target_assemblies`; the default configuration only targets `Assembly-CSharp.dll`.

## Evidence cards and patch format

Each card contains the member identity and available native evidence. It may include a reduced decompilation, a bounded raw excerpt, field/property declarations, string literals, and a linked async `MoveNext` function when recognized. Evidence may be incomplete; a card is **not a proof of source-level equivalence**.

A patch is a JSON array, with one entry per member. `body` contains **only the inside of the method**, without enclosing braces:

```json
[
  {
    "file": "Game/Counter.cs",
    "member": "GetValue",
    "params": 0,
    "body": "return _value;",
    "note": "Reads the indexed instance field."
  }
]
```

For overloaded methods, include the precise `signature` from the card when available. Run `apply --dry-run` before writing. The normal `apply` command refuses to replace members whose evidence block has already been removed unless explicitly overridden with `--allow-restored`.

**Important:** keep the generated `<patch>.evidence.json` snapshot. Pass it to `verify --snapshot` so review can still refer to the original evidence after body replacement.

## Verification and its limits

The queue separates `partial`, `stub`, `marker_only`, `empty_silent`, `legit_empty`, and `restored` members. Verification can flag leftover evidence, unfinished markers, placeholders, mechanical native constructs, empty bodies, and weak anchor coverage.

`build` compiles a temporary project against reference DLLs, excluding assemblies being reconstructed from source to avoid duplicate type definitions. The **configured gate is based on syntax-class errors and residual markers**, not on full compilation success or behavioral equivalence. Always inspect the compiler exit code and all reported errors separately. A passed heuristic check does not prove that a method behaves like the original binary.

## Known limitations

- **No embedded model runner or autonomous reconstruction.** Cards leave this tool; patch JSON comes back from your external workflow. There is no built-in batch/class restore scheduler.
- **No built-in IDA/Ghidra automation.** Export decompiled functions first. Native reduction and virtual-call extraction are primarily tuned for Hex-Rays-style C.
- **No complete Unity project recovery.** Scene, prefab, asset, shader, and package restoration require separate tooling.
- **Generics and shared native bodies are not fully resolved.** Overloads, shared generic implementations, lambdas, and state machines require additional checking. Do not treat an inferred match as authoritative.
- **Native-to-managed joins are evidence-dependent.** Address alignment can fail or be ambiguous; the image base may remain unknown rather than being guessed.
- **Verification is heuristic.** Anchor coverage, residue checks, and Roslyn diagnostics cannot establish full semantic equivalence.
- Platform coverage and Cpp2IL variants beyond the documented workflow require validation with your target build.

## Project layout

```text
forge/                 Core CLI and libraries
  unity/               APK/XAPK payload and metadata handling
  dump/                Dump parsing
  index/               Managed/native symbol index
  native/              Pseudo-C indexing, reduction, runtime helpers
  csharp/              Member parsing and body operations
  restore/             Queue, evidence cards, patch application, verification
  build/               Diagnostic C# project and compiler output
  report/              Progress reporting
  prompts/             Model-facing member contracts
skills/il2cpp-forge/   Agent skill instructions
examples/              Example project configuration
docs/                  Architecture and migration notes
tests/                 Python unittest sources
AGENTS.md              Rules for agents modifying reconstructed code
LICENSE                MIT license
```

For implementation decisions, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). For migration from the older toolkit, see [`docs/MIGRATION_FROM_V5.md`](docs/MIGRATION_FROM_V5.md). Agents should read [`AGENTS.md`](AGENTS.md) before generating or applying code.

## License

`il2cpp-forge` is licensed under the **MIT License**. See [`LICENSE`](LICENSE). External tools, game binaries, and third-party assets have their **own** licenses and rights; they are not covered by this project's MIT license.
