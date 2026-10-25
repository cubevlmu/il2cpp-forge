---
name: il2cpp-forge
description: Reconstruct C# from an authorised Unity IL2CPP build with evidence-gated tooling — locate the payload, dump metadata, index methods against native code, emit per-member evidence cards, apply and verify restored bodies, and gate progress on syntax errors and residue.
---

# il2cpp-forge

Use this skill when the task is "turn this Unity IL2CPP game/app into readable,
compilable C#", or any part of it: dumping, indexing, restoring individual
methods, checking that a restored body is faithful, or reporting what is left.

## Before anything

1. Read `AGENTS.md`. It is the contract: evidence first, no silent closure, no
   reflection scaffolding, logic equivalence over 1:1.
2. Confirm the target is authorised. Do not upload binaries or dumps anywhere.
3. `il2cpp-forge doctor` — check the payload, dump, native corpus, source tree
   and reference assemblies are all configured.

## The pipeline

| stage | command | what it produces |
| --- | --- | --- |
| locate | `detect --input game.apk` | `libil2cpp.so` + `global-metadata.dat`, metadata version |
| dump | `dump --exe <Il2CppDumper>` | `dump.cs`, `script.json`, `DummyDll/` |
| index | `index --native-dir <decompiled C>` | `index/methods.jsonl`, `types.jsonl`, `symbols.json`, native join |
| queue | `queue --src work/src` | open members by kind: `partial`, `stub`, `marker_only`, `empty_silent` |
| cards | `cards --src work/src --limit N` | one JSON/Markdown card per member |
| restore | *model, outside the tool* | JSON patch entries |
| apply | `apply --patch ... --dry-run` then without | bodies written, evidence snapshotted |
| verify | `verify --patch ...` | `EVIDENCE`/`MARKER`/`PLACEHOLDER`/`SILENT_CLOSURE`/`LOW_COVERAGE` |
| build | `build --src ... --refs dump/DummyDll` | syntax-class errors, residue, gate |

## Rules that matter in practice

- **One card, one member.** Do not batch unrelated members into one answer, and
  never let a model see a whole file.
- **Async logic lives in `MoveNext`.** The card follows it automatically. If a
  card has no `evidence.movenext` for an async method, the corpus is missing it —
  say so instead of reconstructing the trampoline.
- **`StringLiteral_N` is unreadable.** Cards resolve literals via
  `script.json`. If a literal is unresolved, do not guess its text.
- **Never clear the queue by deleting evidence.** Clearing means writing bodies,
  member by member.
- **Progress is syntax errors + residue + silent closures**, not the total error
  count. `build` prints all four; the gate fails on syntax and residue.

## Output contract for a restore round

```
restored: <n> members in <m> files
verify:   ok=<n> failed=<n> (<issue>:<count>, ...)
build:    syntax=<before>-><after>  residue=<before>-><after>  errors=<before>-><after>
left open: <n> (<kind> breakdown), reasons
```

Nothing else. No narrative about the process, no claims about "original source".
