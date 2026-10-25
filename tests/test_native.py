import tempfile
import unittest
from pathlib import Path

from forge.native import decompiled as dec
from forge.native import helpers as hl
from forge.native.decompiled import FunctionIndex, Reducer

FUNCTION = """//----- (0000000000123400) ----------------------------------------------------
void Game_Panel__Update(Game_Panel_o *this, const MethodInfo *method)
{
  __int64 v3; // x1
  if ( (byte_1A2B & 1) == 0 )
  {
    sub_52A76A0((unsigned __int64 *)&Method_Game_Panel_Update__, v3);
  }
  this->fields._count = this->fields._count + 1;
  sub_52A764C(&this->fields._count);
  Game_Audio__Play((Game_Audio_o *)this->fields._audio, "click", 0LL);
  if ( this->fields._muted )
  {
    Game_Log__Warn((Game_Log_o *)this->fields._log, "muted", method);
  }
  return;
}

//----- (0000000000123600) ----------------------------------------------------
void Game_Panel__OnClick_d__4__MoveNext(Game_Panel__OnClick_d__4_o *this, const MethodInfo *method)
{
  Game_Server__Send((Game_Server_o *)this->fields._server, this->fields._count);
  return;
}
"""

HELPERS = hl.HelperTable.from_config(
    {
        "class_init": ["sub_52A76A0"],
        "write_barrier": ["sub_52A764C"],
        "interface_dispatch": ["sub_527D950"],
    }
)


class ReduceTest(unittest.TestCase):
    def setUp(self):
        self.reducer = Reducer(plumbing=HELPERS.plumbing())

    def test_runtime_helpers_are_dropped(self):
        lines = self.reducer.reduce(dec.function_body(FUNCTION))
        text = "\n".join(lines)
        self.assertNotIn("sub_52A76A0", text)
        self.assertNotIn("sub_52A764C", text)
        self.assertNotIn("byte_1A2B", text)

    def test_business_statements_survive(self):
        text = "\n".join(self.reducer.reduce(dec.function_body(FUNCTION)))
        self.assertIn("Game_Audio__Play", text)
        self.assertIn("_count", text)

    def test_guard_with_business_body_survives(self):
        text = "\n".join(self.reducer.reduce(dec.function_body(FUNCTION)))
        self.assertIn("if ( this->fields._muted )", text)
        self.assertIn("Game_Log__Warn", text)

    def test_declarations_are_dropped(self):
        text = "\n".join(self.reducer.reduce(dec.function_body(FUNCTION)))
        self.assertNotIn("__int64 v3", text)

    def test_class_init_guard_strip(self):
        lines = dec.function_body(FUNCTION).splitlines()
        stripped = dec.strip_class_init_guard(lines)
        text = "\n".join(stripped)
        self.assertNotIn("byte_1A2B", text)
        self.assertIn("Game_Audio__Play", text)

    def test_statements_tokenises_nested_calls(self):
        tokens = dec.statements("a(b(c), d); e = 1;")
        self.assertEqual(tokens, ["a(b(c), d)", "e = 1"])

    def test_brace_less_branch_is_split_from_its_head(self):
        tokens = dec.statements("if ( a ) x = 1; else y = 2;")
        self.assertEqual(
            tokens, ["if ( a )", "x = 1", "else", "y = 2"]
        )

    def test_split_control_handles_else_if_chain(self):
        self.assertEqual(
            dec.split_control("else if ( b ) z = 3"),
            ["else if ( b )", "z = 3"],
        )

    def test_brace_less_branch_pairs_with_its_else(self):
        source = """
        void f() {
          if ( parameter->fields._item ) afterLevelText = parameter->fields._x;
          else v77 = (System_String_o *)" ";
        }
        """
        text = "\n".join(self.reducer.reduce(source))
        self.assertIn("if ( parameter->fields._item )", text)
        self.assertIn("else", text)
        self.assertEqual(text.count("else"), 1)

    def test_field_guard_survives_without_anchors(self):
        # ``if (!x) goto LABEL_5`` is the native null check; dropping it hides a
        # branch the C# must keep even though the body holds no anchors.
        source = """
        void f() {
          if ( !parameter->fields._item ) goto LABEL_78;
          Game_Audio__Play();
        }
        """
        text = "\n".join(self.reducer.reduce(source))
        self.assertIn("if ( !parameter->fields._item )", text)
        self.assertIn("// no anchors in this branch", text)

    def test_label_does_not_swallow_the_next_statement(self):
        # A label has no trailing ``;``, so it arrives glued to the statement it
        # introduces; left glued, that statement loses its terminator and is
        # never scored as a seed.
        tokens = dec.statements("LABEL_26:\n  beforeLevelText = this->fields._beforeLevelText;")
        self.assertEqual(
            tokens, ["LABEL_26:", "beforeLevelText = this->fields._beforeLevelText"]
        )
        text = "\n".join(self.reducer.reduce("void f() {\nLABEL_26:\n  _t = this->fields._a;\n}"))
        self.assertIn("LABEL_26:", text)
        self.assertIn("_t = this->fields._a;", text)

    def test_pointer_temporary_fields_are_not_behaviour(self):
        # Hex-Rays spells pointer temporaries ``v65->fields.X``; that is the same
        # struct shuffling as ``v65.fields.X`` and carries no behaviour.
        source = """
        void f() {
          v65 = v64;
          v66 = v65->fields._Id_k__BackingField;
          Game_Audio__Play(this->fields._count);
        }
        """
        text = "\n".join(self.reducer.reduce(source))
        self.assertNotIn("_Id_k__BackingField", text)
        self.assertIn("Game_Audio__Play", text)

    def test_else_arm_keeps_its_condition(self):
        # An ``else`` whose ``if`` arm holds no anchors must not be emitted bare:
        # a branch with no condition reads as a decompiler artefact, not evidence.
        source = """
        void f() {
          if ( this->fields._muted ) { v1 = v2->fields._x; } else { Game_Log__Warn(); }
        }
        """
        text = "\n".join(self.reducer.reduce(source))
        self.assertIn("if ( this->fields._muted )", text)
        self.assertIn("else", text)
        self.assertIn("Game_Log__Warn", text)
        self.assertEqual(text.count("else"), 1)

    def test_empty_branch_is_marked_not_blanked(self):
        source = """
        void f() {
          if ( this->fields._muted ) { v1 = v2->fields._x; } else { Game_Log__Warn(); }
        }
        """
        text = "\n".join(self.reducer.reduce(source))
        self.assertIn("// no anchors in this branch", text)

    def test_framework_calls_are_not_business_when_roles_say_so(self):
        reducer = Reducer(
            plumbing=HELPERS.plumbing(),
            symbol_roles={"System_String": "framework", "Game_Audio": "business"},
        )
        text = "\n".join(reducer.reduce(dec.function_body(FUNCTION)))
        self.assertIn("Game_Audio__Play", text)


class FunctionIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "Game").mkdir()
        (self.root / "Game" / "Panel.c").write_text(FUNCTION, encoding="utf-8")
        (self.root / "Game" / "Other.c").write_text(
            "//----- (0000000000123999) ------------------------------------\n"
            "void Game_Other__Run(Game_Other_o *this)\n{\n  return;\n}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_and_read(self):
        index = FunctionIndex.build(self.root)
        self.assertGreaterEqual(len(index), 3)
        text = index.read("Game_Panel__Update")
        self.assertIn("Game_Audio__Play", text)
        self.assertTrue(text.startswith("//----- (0000000000123400)"))

    def test_address_index(self):
        index = FunctionIndex.build(self.root)
        self.assertEqual(index.by_address.get(0x123400), "Game_Panel__Update")

    def test_movenext(self):
        index = FunctionIndex.build(self.root)
        self.assertEqual(
            index.find_movennext("Game_Panel", "OnClick"), "Game_Panel__OnClick_d__4__MoveNext"
        )
        self.assertIsNone(index.find_movennext("Game_Panel", "Missing"))

    def test_cache_roundtrip(self):
        cache = self.root / "cache" / "functions.tsv"
        first = FunctionIndex.build(self.root, cache_file=cache)
        self.assertTrue(cache.is_file())
        second = FunctionIndex.build(self.root, cache_file=cache)
        self.assertEqual(set(first.functions), set(second.functions))
        self.assertEqual(second.by_address.get(0x123400), "Game_Panel__Update")

    def test_cache_invalidates_on_change(self):
        cache = self.root / "cache" / "functions.tsv"
        FunctionIndex.build(self.root, cache_file=cache)
        (self.root / "Game" / "New.c").write_text(
            "void Game_New__Go(Game_New_o *this)\n{\n  return;\n}\n", encoding="utf-8"
        )
        rebuilt = FunctionIndex.build(self.root, cache_file=cache)
        self.assertIn("Game_New__Go", rebuilt.functions)


class HelperLearningTest(unittest.TestCase):
    def test_symbols_are_authoritative(self):
        table = hl.learn_from_symbols(
            {0x52A76A0: "il2cpp_codegen_initialize_method"},
            {0x52A76A0: "sub_52A76A0"},
        )
        self.assertEqual(table.roles.get("class_init"), ["sub_52A76A0"])
        self.assertIn("sub_52A76A0", table.plumbing())

    def test_corpus_shape_learning(self):
        chunk = "\n".join(
            [
                "if ( (byte_9A & 1) == 0 )",
                "{",
                "  sub_AAAAAA(&Method_Foo_Bar__, v1);",
                "}",
            ]
            * 20
        )
        table = hl.learn_from_corpus([chunk], min_hits=5)
        self.assertIn("sub_AAAAAA", table.roles.get("class_init", []))

    def test_parse_symbol_dump(self):
        parsed = hl.parse_symbol_dump("0000000005d1e3f0 T il2cpp_codegen_initialize_method\n")
        self.assertEqual(parsed[0x5D1E3F0], "il2cpp_codegen_initialize_method")

    def test_role_matching(self):
        self.assertEqual(hl.role_for_symbol("il2cpp_gc_wbarrier_set_field"), "write_barrier")
        self.assertIsNone(hl.role_for_symbol("memcpy"))


if __name__ == "__main__":
    unittest.main()
