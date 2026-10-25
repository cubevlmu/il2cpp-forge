# Agent contract — IL2CPP reconstruction with `il2cpp-forge`

This file is the operating contract for any model or human driving this toolkit.
Read it before touching a tree. It is short on purpose; every rule here exists
because breaking it produced work that had to be thrown away.

## 1. What this tool is

`il2cpp-forge` turns an authorised Unity IL2CPP build into a reconstructable C#
tree and then keeps that reconstruction honest:

```
payload → dump → index → queue → cards → (model) → patch → apply → verify → build
```

Every stage writes plain JSON/JSONL into the project directory. Nothing is
hidden in a database, and no stage needs network access except the model call
that happens *outside* the tool.

## 2. The evidence rule

**Generated C# is a hypothesis about behaviour, supported by native evidence. It
is never "the original source".**

- Every restored body must be explainable by the native code in its card: the
  calls it makes, the fields it writes, the literals it uses.
- Inferred detail is allowed. The standard is **logic equivalence, not 1:1
  identity**. Every inferred point carries `// INFER: <reason>`.
- `// INFER:` is an *accepted* marker. `PARTIAL`, `DRAFT`, `ASSUMED`,
  `INFER_RESTORE`, `TODO`, `RECOVERY_DRAFT` are *unfinished* markers.
- If the evidence does not support a body, say so: leave the member open, write
  the reason in the card notes, and move on. A wrong body is worse than an open
  one, because it removes the member from the queue while keeping the bug.

## 3. What is forbidden

| Forbidden | Why |
| --- | --- |
| Deleting an `UNRESTORED EVIDENCE` block to "clear the queue" | changes no behaviour and hides the member forever; the queue is measured against evidence, so deleting evidence is silent closure |
| Reflection scaffolding (`GetMethods().OrderBy(MetadataToken)[slot].Invoke`) | passes every residue check, dies under IL2CPP stripping |
| Placeholder bodies (`throw new NotImplementedException`, `return default`) presented as done | counted as `PLACEHOLDER`/`stub` by `verify` |
| Replacing a whole class to patch one method | destroys attributes, field layout, overloads |
| Editing the original dump, the APK, or `libil2cpp.so` | the evidence must stay byte-identical |
| Inventing third-party package versions from `AssemblyVersion` | a DLL version is not a NuGet version |

## 4. Workflow

```bash
# 1. what is open, and what shape is it?
il2cpp-forge queue --src work/src --csv work/open.csv

# 2. one card per member: signature, field layout, reduced native, literals, anchors
il2cpp-forge cards --src work/src --limit 20 --out work/cards --markdown

# 3. answer each card as JSON: {member, params, body, confidence, notes}
#    body is inner statements only, no outer braces

# 4. land it safely
il2cpp-forge apply --patch work/patch.json --src work/src --dry-run
il2cpp-forge apply --patch work/patch.json --src work/src --backup work/backup

# 5. judge it
il2cpp-forge verify --patch work/patch.json --src work/src --json
il2cpp-forge build  --src work/src --refs dump/DummyDll --json
```

Rules for step 3:

- Match by `(member, param count, parameter types)`. Never by line number.
- Use only names that exist in the card's signature and field layout.
- Async methods: the business logic is in `MoveNext`. The card already follows
  it. Do not reconstruct the builder trampoline.
- IL2CPP runtime helpers (class init, write barrier, interface dispatch,
  null/bounds throw) are language machinery. They never appear in C#. The card's
  reduced native already dropped them.
- Literals come from the card. Never type a string that is not in the evidence.

## 5. How to judge progress

Never judge by total error count. A tree can gain errors because a new body made
the compiler check more code.

Judge by, in order:

1. **syntax-class errors must be 0** (`forge build` reports `syntax_errors`).
   Anything else is unmeasurable until this is zero.
2. **residue must be 0** — no `Il2CppObject`, `sub_XXXXXX`, `->fields`,
   `StringLiteral_N`, `MECHANICAL_TRANSLATION` left in live code.
3. **no silent closures** — `forge verify` reports `SILENT_CLOSURE` when the
   native has anchors and the body mentions none.
4. **open-member count** from `queue`, split by kind. `marker_only` and
   `empty_silent` are the two kinds that fake a clean tree.

## 6. Reporting

When you finish a round, report only:

- files touched, members restored, and the `kind` they had before;
- verification result (`ok` / `failed` with issue counts);
- build delta: syntax errors before/after, residue before/after, total errors
  with the codes that appeared and disappeared;
- members you left open and the evidence that was missing.

Do not report "all done" unless `queue` says the open set is empty *and* you
have read the `marker_only` and `empty_silent` entries yourself.
