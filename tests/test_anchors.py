import unittest

from forge.native import anchors as al
from forge.native.decompiled import Reducer


def rules(**roles):
    return al.AnchorRules(reducer=Reducer(symbol_roles=roles))


LINES = [
    "System_String__op_Equality(a, b);",
    "Game_Panel__Refresh(this);",
    "this->fields._count = 1;",
    "x = this->fields._cached;",
    'Game_Audio__Play("click");',
    "v.fields.mode = 2;",
    "v.fields.Item1 = 3;",
    "x.fields._x_k__BackingField = 4;",
]


class AnchorTest(unittest.TestCase):
    def test_framework_calls_are_not_anchors(self):
        found = al.business_anchors(LINES, "Update", rules(**{"System_String": "framework"}))
        self.assertNotIn("op_Equality", found.calls)
        self.assertIn("Refresh", found.calls)
        self.assertIn("Play", found.calls)

    def test_self_name_is_not_an_anchor(self):
        found = al.business_anchors(["Game_Panel__Refresh(this);"], "Refresh", rules())
        self.assertNotIn("Refresh", found.calls)

    def test_generic_tail_is_not_an_anchor(self):
        found = al.business_anchors(
            ["Game_Panel__Init_Int32Enum_(this);"], "Update", rules()
        )
        self.assertEqual(found.calls, [])

    def test_only_written_fields_count(self):
        found = al.business_anchors(LINES, "Update", rules())
        self.assertIn("_count", found.fields)
        self.assertNotIn("_cached", found.fields)

    def test_struct_slots_are_ignored(self):
        found = al.business_anchors(LINES, "Update", rules())
        for slot in ("mode", "Item1", "_x_k__BackingField"):
            self.assertNotIn(slot, found.fields)

    def test_literals(self):
        found = al.business_anchors(LINES, "Update", rules())
        self.assertIn('"click"', found.literals)

    def test_coverage_and_verdicts(self):
        found = al.business_anchors(LINES, "Update", rules())
        live = "Refresh(); Play(\"click\"); _count = 1;"
        hit, total, missing = al.coverage(live, found)
        self.assertGreater(hit, 0)
        self.assertEqual(total, found.total)
        self.assertIn("silent_closure", al.score("", found)["verdict"])
        self.assertEqual(al.score(live, found)["verdict"], "covered")

    def test_low_coverage(self):
        found = al.business_anchors(LINES, "Update", rules())
        result = al.score("Refresh();", found, min_coverage=0.9)
        self.assertEqual(result["verdict"], "low_coverage")

    def test_no_anchors_is_not_a_failure(self):
        result = al.score("return 1;", al.Anchors())
        self.assertEqual(result["verdict"], "no_anchors")


if __name__ == "__main__":
    unittest.main()
