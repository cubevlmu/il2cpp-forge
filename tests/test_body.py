import unittest

from forge.csharp import body as bu

EVIDENCE_FIRST = """{
\t// ==== UNRESTORED EVIDENCE ====
\t// status=blocked rva=0x1234 source: Game/Panel.c
\t// C | v1 = sub_52A76A0(&Method_X);
\t// C | return _count;
\t// ==== END UNRESTORED EVIDENCE ====
\treturn _count + 1;
}"""

CODE_FIRST = """{
\treturn _count + 1;
\t// ==== UNRESTORED EVIDENCE ====
\t// status=blocked
\t// C | return _count;
\t// ==== END UNRESTORED EVIDENCE ====
}"""

PROSE_TRAP = """{
\t// BEGIN MECHANICAL C TRANSLATION
\t// Original C evidence preserved in UNRESTORED EVIDENCE block above.
\tthrow new NotImplementedException();
}"""


class EvidenceTest(unittest.TestCase):
    def test_block_before_code(self):
        inner = bu.inner_of(EVIDENCE_FIRST).strip()
        self.assertEqual(inner, "return _count + 1;")

    def test_block_after_code(self):
        inner = bu.inner_of(CODE_FIRST).strip()
        self.assertEqual(inner, "return _count + 1;")

    def test_prose_mention_does_not_open_a_block(self):
        # The banner regex must not treat prose as a boundary; if it did, the
        # throw below would be swallowed and the body would look empty.
        inner = bu.inner_of(PROSE_TRAP).strip()
        self.assertIn("NotImplementedException", inner)

    def test_code_of_skips_comments_and_braces(self):
        lines = bu.code_of(CODE_FIRST)
        self.assertEqual(lines, ["\treturn _count + 1;"])

    def test_evidence_meta(self):
        meta = bu.evidence_meta(EVIDENCE_FIRST)
        self.assertEqual(meta["status"], "blocked")
        self.assertEqual(meta["rva"], "0x1234")
        self.assertEqual(meta["source"], "Game/Panel.c")

    def test_infer_marker_survives_strip(self):
        body = "{\n\t// INFER: branch taken from the guard\n\treturn 1;\n}"
        self.assertIn("INFER:", bu.inner_of(body))

    def test_unfinished_marker_is_removed(self):
        body = "{\n\t// RECOVERY_PARTIAL: only the happy path\n\treturn 1;\n}"
        self.assertNotIn("RECOVERY_PARTIAL", bu.inner_of(body))

    def test_classify_flags(self):
        cases = {
            "{\n\tthrow new NotImplementedException();\n}": "PLACEHOLDER",
            "{\n\tvar x = obj->fields.count;\n}": "MECHANICAL",
            "{\n\t// RECOVERY_PARTIAL: x\n\treturn 1;\n}": "MARKER",
            "{\n}": "EMPTY",
        }
        for text, expected in cases.items():
            verdict = bu.classify(text, name="Run")
            self.assertIn(expected, verdict.issues, text)

    def test_classify_accepts_legit_empty(self):
        self.assertTrue(bu.classify("{\n}", name="Dispose").ok)

    def test_reindent(self):
        text = bu.reindent("if (x)\n{\nreturn 1;\n}", "\t")
        self.assertEqual(text, "\t\tif (x)\n\t\t{\n\t\treturn 1;\n\t\t}")

    def test_custom_banner(self):
        policy = bu.BodyPolicy(banner="NATIVE EVIDENCE")
        body = "{\n\t// NATIVE EVIDENCE\n\t// C | x\n\t// END NATIVE EVIDENCE\n\treturn 2;\n}"
        self.assertTrue(policy.has_evidence(body))
        self.assertEqual(bu.inner_of(body, policy).strip(), "return 2;")


if __name__ == "__main__":
    unittest.main()
