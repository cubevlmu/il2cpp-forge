import json
import tempfile
import unittest
from pathlib import Path

from forge.csharp import body as bu
from forge.restore import apply as applylib
from forge.restore import queue as queuelib
from forge.restore import verify as verifylib
from forge.restore.cards import CardContext

SOURCE = '''namespace Foo
{
    public class Bar
    {
        public void Done()
        {
            // ==== UNRESTORED EVIDENCE ====
            // status=blocked rva=0x1000 source: Foo/Bar.c
            // C | Game_Server__Send(this->fields._server, 1);
            // ==== END UNRESTORED EVIDENCE ====
            throw new NotImplementedException();
        }

        public void Finished()
        {
            Game_Server__Send(_server, 2);
        }

        public void Empty() { }

        public void Dispose() { }
    }
}
'''


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        (self.src / "Foo").mkdir(parents=True)
        (self.src / "Foo" / "Bar.cs").write_text(SOURCE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_queue_classification(self):
        queue = queuelib.scan(self.src)
        kinds = {(e.member, e.kind) for e in queue.entries}
        self.assertIn(("Done", "stub"), kinds)
        self.assertNotIn("Finished", [e.member for e in queue.entries])
        self.assertIn(("Empty", "empty_silent"), kinds)
        self.assertNotIn("Dispose", [e.member for e in queue.entries])

    def test_marker_only_is_visible(self):
        path = self.src / "Foo" / "Baz.cs"
        path.write_text(
            "namespace Foo\n{\n    public class Baz\n    {\n"
            "        public void Run()\n        {\n"
            "            // RECOVERY_PARTIAL: only the happy path\n"
            "            return;\n        }\n    }\n}\n",
            encoding="utf-8",
        )
        queue = queuelib.scan(self.src)
        kinds = {(e.member, e.kind) for e in queue.entries}
        self.assertIn(("Run", "marker_only"), kinds)

    def test_apply_dry_run_changes_nothing(self):
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "Game_Server__Send(_server, 1);"}]
            ),
            encoding="utf-8",
        )
        before = (self.src / "Foo" / "Bar.cs").read_text(encoding="utf-8")
        result = applylib.apply_patch(patch, self.src, dry_run=True)
        self.assertEqual(result.written, 1)
        self.assertEqual((self.src / "Foo" / "Bar.cs").read_text(encoding="utf-8"), before)

    def test_apply_writes_and_snapshots(self):
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "Game_Server__Send(_server, 1);"}]
            ),
            encoding="utf-8",
        )
        result = applylib.apply_patch(patch, self.src, backup_dir=self.root / "backup")
        self.assertEqual(result.written, 1)
        text = (self.src / "Foo" / "Bar.cs").read_text(encoding="utf-8")
        self.assertNotIn("UNRESTORED EVIDENCE", text)
        self.assertIn("Game_Server__Send(_server, 1);", text)
        self.assertTrue((self.root / "backup" / "Foo" / "Bar.cs").is_file())
        self.assertTrue(result.snapshot_path and result.snapshot_path.is_file())

        # Second application must refuse: the member no longer carries evidence.
        again = applylib.apply_patch(patch, self.src)
        self.assertEqual(again.written, 0)
        self.assertTrue(any("NO_EVIDENCE" in p for p in again.problems))

    def test_apply_keeps_the_blank_line_between_members(self):
        # ``body_end`` is exclusive; treating it as inclusive ate the newline
        # after the closing brace and glued the next member to it.
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "Game_Server__Send(_server, 1);"}]
            ),
            encoding="utf-8",
        )
        applylib.apply_patch(patch, self.src)
        text = (self.src / "Foo" / "Bar.cs").read_text(encoding="utf-8")
        self.assertIn("}\n\n        public void Finished()", text)
        self.assertIn("Game_Server__Send(_server, 1);\n        }", text)
        self.assertNotIn("\t", text)
        self.assertEqual(text.count("Game_Server__Send"), 2)

    def test_apply_removes_a_marker_above_the_member(self):
        # A marker written above the signature survives a body replacement and
        # then contradicts the finished body for every later reader.
        path = self.src / "Foo" / "Bar.cs"
        path.write_text(
            SOURCE.replace(
                "        public void Done()",
                "        // PARTIALLY_RESTORED: still guessing\n        public void Done()",
            ),
            encoding="utf-8",
        )
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "Game_Server__Send(_server, 3);"}]
            ),
            encoding="utf-8",
        )
        applylib.apply_patch(patch, self.src)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("PARTIALLY_RESTORED", text)
        self.assertIn("Game_Server__Send(_server, 3);", text)

    def test_verify_after_apply(self):
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "Game_Server__Send(_server, 1);"}]
            ),
            encoding="utf-8",
        )
        result = applylib.apply_patch(patch, self.src)
        snapshot = verifylib.load_snapshot(result.snapshot_path)
        context = CardContext(policy=bu.BodyPolicy())
        verdicts = verifylib.verify(
            [{"file": "Foo/Bar.cs", "member": "Done", "params": 0}],
            self.src,
            context,
            snapshot=snapshot,
        )
        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0].ok, verdicts[0].issues)
        self.assertEqual(verdicts[0].total, 1)
        self.assertEqual(verdicts[0].hit, 1)

    def test_verify_flags_placeholder(self):
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "throw new NotImplementedException();"}]
            ),
            encoding="utf-8",
        )
        result = applylib.apply_patch(patch, self.src)
        snapshot = verifylib.load_snapshot(result.snapshot_path)
        verdicts = verifylib.verify(
            [{"file": "Foo/Bar.cs", "member": "Done", "params": 0}],
            self.src,
            CardContext(policy=bu.BodyPolicy()),
            snapshot=snapshot,
        )
        self.assertIn("PLACEHOLDER", verdicts[0].issues)
        self.assertIn("SILENT_CLOSURE", verdicts[0].issues)

    def test_verify_detects_silent_closure(self):
        patch = self.root / "patch.json"
        patch.write_text(
            json.dumps(
                [{"file": "Foo/Bar.cs", "member": "Done", "params": 0,
                  "body": "return;"}]
            ),
            encoding="utf-8",
        )
        result = applylib.apply_patch(patch, self.src)
        snapshot = verifylib.load_snapshot(result.snapshot_path)
        verdicts = verifylib.verify(
            [{"file": "Foo/Bar.cs", "member": "Done", "params": 0}],
            self.src,
            CardContext(policy=bu.BodyPolicy()),
            snapshot=snapshot,
        )
        self.assertIn("SILENT_CLOSURE", verdicts[0].issues)


class TypeContextTest(unittest.TestCase):
    """The queue reports a bare class name; the index is keyed by full name."""

    def setUp(self):
        self.types = {
            "Sirius.AD.ADFactory": {
                "full_name": "Sirius.AD.ADFactory",
                "kind": "class",
                "base_types": ["MonoBehaviour"],
                "fields": [
                    {"name": "_ad", "type": "IAD", "offset": 16, "is_static": False}
                ],
                "properties": [],
            },
            "Other.ADFactory": {
                "full_name": "Other.ADFactory",
                "kind": "class",
                "base_types": [],
                "fields": [{"name": "_other", "type": "int", "offset": 16}],
                "properties": [],
            },
        }

    def test_bare_name_resolves(self):
        from forge.restore.cards import _type_context

        record = _type_context("ADFactory", self.types, hint="AD/ADFactory.cs")
        self.assertEqual(record["full_name"], "Sirius.AD.ADFactory")
        self.assertEqual(record["fields"][0]["name"], "_ad")

    def test_namespace_is_disambiguated_by_path(self):
        from forge.restore.cards import _type_context

        record = _type_context("ADFactory", self.types, hint="Other/ADFactory.cs")
        self.assertEqual(record["full_name"], "Other.ADFactory")

    def test_unknown_owner_is_empty_not_wrong(self):
        from forge.restore.cards import _type_context

        self.assertEqual(_type_context("Missing", self.types), {})

    def test_packaged_contract_is_loaded(self):
        from forge.restore.cards import prompt_text

        text = prompt_text("restore_member.md")
        self.assertIn("INFER:", text)


if __name__ == "__main__":
    unittest.main()


class EnrichTest(unittest.TestCase):
    """A wrong native pointer is worse than a missing one."""

    def setUp(self):
        self.methods = [
            {"name": "Show", "params": [], "owner": "Features.Audition.AuditionTutorialUseCase",
             "native_name": "Features_Audition_UseCase_AuditionTutorialUseCase__Show",
             "native_file": "Audition/AuditionTutorialUseCase.c", "rva": 1},
            {"name": "Show", "params": [], "owner": "Sirius.CharacterEnhance.CharacterEnhancePanelBase",
             "native_name": "Sirius_CharacterEnhance_CharacterEnhancePanelBase__Show",
             "native_file": "CharacterEnhance/CharacterEnhancePanelBase.c", "rva": 2},
        ]

    def test_owner_narrows_an_ambiguous_name(self):
        from forge.cli import _enrich

        entry = {"file": "CharacterEnhance/CharacterEnhancePanelBase.cs", "member": "Show", "params": 0}
        _enrich([entry], self.methods)
        self.assertEqual(
            entry["native_name"], "Sirius_CharacterEnhance_CharacterEnhancePanelBase__Show"
        )
        self.assertNotIn("native_ambiguous", entry)

    def test_unresolvable_name_is_left_unlinked(self):
        from forge.cli import _enrich

        entry = {"file": "Other/Unknown.cs", "member": "Show", "params": 0}
        _enrich([entry], self.methods)
        self.assertNotIn("native_name", entry)
        self.assertEqual(entry["native_ambiguous"], 2)

    def test_unique_name_is_linked(self):
        from forge.cli import _enrich

        methods = [{"name": "SetData", "params": [1], "owner": "A.B",
                    "native_name": "A_B__SetData", "native_file": "B.c", "rva": 3}]
        entry = {"file": "X.cs", "member": "SetData", "params": 1}
        _enrich([entry], methods)
        self.assertEqual(entry["native_name"], "A_B__SetData")

    def test_queue_does_not_link_an_ambiguous_name(self):
        from forge.restore import queue as ql

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Other").mkdir()
            (root / "Other" / "Unknown.cs").write_text(
                "namespace Other\n{\n    public class Unknown\n    {\n"
                "        public void Show() { }\n    }\n}\n",
                encoding="utf-8",
            )
            queue = ql.scan(root, self.methods, bu.BodyPolicy())
        entry = [e for e in queue.entries if e.member == "Show"][0]
        self.assertEqual(entry.native_name, "")
        self.assertEqual(entry.note, "ambiguous:2")
