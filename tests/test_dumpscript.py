import tempfile
import unittest
from pathlib import Path

from forge.dump import dumpscript as ds

FIXTURE = '''// Image 0: Game.dll - 0
// Image 1: System.dll - 2

// Namespace: Game.UI
public class Panel : MonoBehaviour // TypeDefIndex: 0
{
\t// Fields
\tprivate int _count; // 0x10
\tpublic const string Title = "Hi";
\t[SerializeField]
\tprivate Text _label; // 0x18
\t[SerializeField]
\tprivate Text _subLabel; // 0x20

\t// Properties
\tpublic int Count { get; set; }

\t// Methods

\t// RVA: 0x1000 Offset: 0x1000 VA: 0x1000
\tpublic void Update() { }

\t// RVA: -1 Offset: -1
\tpublic void Abstract() { }

\t// RVA: 0x1100 Offset: 0x1100
\tpublic void Wrapped(
\t\tint a,
\t\tstring b) { }
\t/* GenericInstMethod :
\t|
\t|-RVA: 0x2000 Offset: 0x2000 VA: 0x2000
\t|-Panel.Wrapped
\t*/

\t// RVA: 0x1200 Offset: 0x1200 Slot: 3
\tpublic static Panel Create(int count, string name) { }
}

// Namespace: System
public class Object // TypeDefIndex: 2
{
\t// Methods
\t// RVA: 0x3000 Offset: 0x3000
\tpublic void ToString() { }
}
'''


class DumpScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "dump.cs"
        path.write_text(FIXTURE, encoding="utf-8")
        cls.model = ds.parse(path)
        cls.panel = [t for t in cls.model.types if t.full_name == "Game.UI.Panel"][0]
        cls.object_type = [t for t in cls.model.types if t.full_name == "System.Object"][0]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_types_and_images(self):
        self.assertEqual(len(self.model.types), 2)
        self.assertEqual(len(self.model.images), 2)
        self.assertEqual(self.model.method_count(), 5)

    def test_assembly_comes_from_typedef_range(self):
        self.assertEqual(self.panel.assembly, "Game.dll")
        self.assertEqual(self.object_type.assembly, "System.dll")

    def test_fields(self):
        names = {f.name: f for f in self.panel.fields}
        self.assertEqual(names["_count"].offset, 0x10)
        self.assertTrue(names["Title"].is_const)
        self.assertEqual(names["Title"].value, '"Hi"')

    def test_attributed_fields_keep_their_attributes_and_offsets(self):
        # ``[SerializeField]`` sits on its own line above the field; losing that
        # path silently empties the layout a card is built from.
        names = {f.name: f for f in self.panel.fields}
        self.assertEqual(names["_label"].offset, 0x18)
        self.assertEqual(names["_subLabel"].offset, 0x20)
        self.assertEqual(names["_label"].attributes, ["SerializeField"])
        self.assertEqual(names["_label"].type, "Text")

    def test_properties(self):
        self.assertEqual(self.panel.properties[0].name, "Count")
        self.assertEqual(self.panel.properties[0].accessors, ["get", "set"])

    def test_methods(self):
        methods = {m.name: m for m in self.panel.methods}
        self.assertEqual(methods["Update"].rva, 0x1000)
        self.assertEqual(methods["Abstract"].rva, 0)  # RVA -1 means "no body"
        self.assertEqual(methods["Create"].slot, 3)
        self.assertTrue(methods["Create"].is_static)
        self.assertEqual(methods["Create"].param_types, ["int", "string"])
        self.assertEqual(methods["Create"].return_type, "Panel")

    def test_wrapped_parameters(self):
        methods = {m.name: m for m in self.panel.methods}
        self.assertEqual(methods["Wrapped"].param_count, 2)

    def test_generic_instances_attach_to_previous_method(self):
        methods = {m.name: m for m in self.panel.methods}
        self.assertEqual(len(methods["Wrapped"].generic_instances), 1)
        self.assertEqual(methods["Wrapped"].generic_instances[0]["rva"], 0x2000)

    def test_mangling(self):
        methods = {m.name: m for m in self.panel.methods}
        self.assertEqual(methods["Update"].mangled(), "Game_UI_Panel__Update")
        self.assertEqual(ds.mangle("A.B.C<D>"), "A_B_C_D_")

    def test_summary(self):
        summary = ds.summary(self.model)
        self.assertEqual(summary["types"], 2)
        self.assertEqual(summary["methods"], 5)


if __name__ == "__main__":
    unittest.main()
