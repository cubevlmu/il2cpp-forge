# Restore one member

You are given **one** method card. Reconstruct the C# body from the native
evidence in the card. You may not read other files, call tools, or search the
internet.

## Input

- `signature` — the managed declaration. This is authoritative for the API shape.
- `type_context` — the owner's fields and properties, with offsets. Use these
  names; do not invent members.
- `current_body` — what is in the tree now (often a stub or a partial attempt).
- `evidence.reduced` — the behaviour-carrying statements of the native body.
  This is the primary source.
- `evidence.raw` — the decompilation, truncated. Use it when `reduced` looks
  ambiguous.
- `evidence.movenext` — for async methods: the state machine that holds the real
  logic. When present, it *is* the body's source.
- `anchors` — calls, written fields and literals a faithful body is expected to
  mention. If your body mentions none of them, it is almost certainly wrong.

## Output

Exactly one JSON object, nothing else:

```json
{
  "member": "<name from the card>",
  "params": <param count>,
  "body": "<statements only, no outer braces>",
  "confidence": "high | medium | low",
  "notes": ["INFER: <why you had to infer this>"]
}
```

`body` is the *inner* text of the method. The tool adds the braces and
re-indents. Use 4 spaces or tabs consistently inside `body`.

## Rules

1. Preserve the signature. Do not change parameter types, names, `ref`/`out`,
   `static`, or the return type.
2. Use only names present in `signature` and `type_context`.
3. Never emit IL2CPP machinery: no `Il2CppObject`, `sub_XXXXXXXX`, `->fields`,
   `0LL`, `StringLiteral_N`, `NativeHelper`, `MECHANICAL_TRANSLATION`.
4. Never use reflection to fake dispatch.
5. Literals must appear in `anchors.literals` or in the raw evidence. Never
   invent user-visible text.
6. Async: write the `async` body as the original author would have. Do not
   reproduce the state machine; `MoveNext` tells you the sequence of awaits,
   assignments and early returns.
7. `throw new NotImplementedException()` is only correct when the native code
   itself throws `NotSupportedException`/`NotImplementedException`. Say so in
   `notes` when you keep it.
8. If the evidence is insufficient for a branch, return `"confidence": "low"`
   and put the gap in `notes`. Do not guess silently.
9. If the member is legitimately empty (empty constructor, empty Unity
   callback), return `"body": ""` and confidence `high`.
10. Do not add comments that describe the decompiler or the recovery process.
    `// INFER: ...` notes are the only comments allowed, and only where you
    actually inferred something.
