# Audit one restored member

You are given one card for a member that is already marked finished. Decide
whether the body is supported by the evidence. You do not rewrite the body.

## Check, in this order

1. **Anchors.** Every call, written field and literal in `anchors` should appear
   in the body — or be explainable (an operator, a property, a language
   construct). Missing anchors are the strongest signal of a wrong body.
2. **Branches.** Count the guards in `evidence.reduced`. The body must have the
   same guards in the same order. A missing `if` is a behaviour change.
3. **Early returns and exceptions.** The native `return` sites and throw sites
   must be reachable in the body.
4. **Literals.** Any literal in the body that is not in the evidence is invented.
5. **Machinery.** Reject `Il2CppObject`, `sub_XXXXXXXX`, `->fields`,
   `StringLiteral_N`, reflection dispatch, `MECHANICAL_TRANSLATION`.
6. **Markers.** `PARTIAL`, `DRAFT`, `ASSUMED`, `INFER_RESTORE`, `TODO` mean the
   member is not finished, whatever the body looks like.

## Output

```json
{
  "member": "<name>",
  "verdict": "supported | weak | unsupported",
  "reasons": ["<one line per finding>"],
  "missing_anchors": ["<names>"]
}
```

- `supported` — every anchor is accounted for and the guards match.
- `weak` — the body is plausible but at least one anchor or guard is
  unaccounted for, or confidence rests on a single call.
- `unsupported` — anchors missing wholesale, or machinery present, or the body
  contradicts the evidence.

Be strict about `unsupported` and honest about `weak`. An audit that passes
everything is worse than no audit.
