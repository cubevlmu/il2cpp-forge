import unittest

from forge.csharp import members as csm

SAMPLE = '''namespace Game.UI
{
\tpublic class Panel : MonoBehaviour
\t{
\t\tprivate int _count;
\t\tpublic (int A, string B) Pair { get; private set; }

\t\tpublic Panel(int count)
\t\t\t: base(count)
\t\t{
\t\t\t_count = count;
\t\t}

\t\tpublic void Simple()
\t\t{
\t\t\t_count++;
\t\t}

\t\tpublic void Inline() { }

\t\tpublic void OneLine() { return; }

\t\tpublic int Read() => _count;

\t\tprivate void Wrapped(
\t\t\tint first,
\t\t\tstring second,
\t\t\tSystem.Collections.Generic.List<int> third
\t\t)
\t\t{
\t\t\t_count = first;
\t\t}

\t\tpublic int Count
\t\t{
\t\t\tget { return _count; }
\t\t\tset { _count = value; }
\t\t}

\t\tvoid IListener.OnEvent(int id)
\t\t{
\t\t\t_count = id;
\t\t}

\t\tpublic void CallSite()
\t\t{
\t\t\tthis.Simple();
\t\t}
\t}
}
'''


class MemberParserTest(unittest.TestCase):
    def setUp(self):
        self.parsed = csm.parse(SAMPLE)

    def find(self, name, params, signature=None):
        return self.parsed.find(name, params, signature)

    def test_plain_method(self):
        member = self.find("Simple", 0)
        self.assertIsNotNone(member)
        self.assertIn("_count++", csm.body_of(member, SAMPLE))

    def test_constructor_with_base_initializer(self):
        member = self.find("Panel", 1)
        self.assertIsNotNone(member)
        self.assertIn("_count = count", csm.body_of(member, SAMPLE))

    def test_inline_body(self):
        member = self.find("Inline", 0)
        self.assertIsNotNone(member)
        self.assertEqual(csm.body_of(member, SAMPLE).strip(), "{ }")

    def test_one_line_body(self):
        member = self.find("OneLine", 0)
        self.assertIsNotNone(member)
        self.assertIn("return;", csm.body_of(member, SAMPLE))

    def test_expression_body_is_not_a_block(self):
        # ``public int Read() => _count;`` has no brace body to patch.
        self.assertIsNone(self.find("Read", 0))

    def test_wrapped_parameter_list(self):
        member = self.find("Wrapped", 3)
        self.assertIsNotNone(member)
        self.assertEqual(csm.param_types(member.signature)[1], "string")

    def test_property_accessors_are_members(self):
        self.assertIsNotNone(self.find("get_Count", 0))
        self.assertIsNotNone(self.find("set_Count", 0))

    def test_tuple_property_not_parsed_as_method(self):
        self.assertIsNone(self.find("Pair", 0))

    def test_explicit_interface_implementation(self):
        member = self.find("OnEvent", 1)
        self.assertIsNotNone(member)

    def test_call_site_is_not_a_member(self):
        names = [m.name for m in self.parsed.members]
        self.assertNotIn("this", names)
        # ``this.Simple();`` must not register a second ``Simple``.
        self.assertEqual(names.count("Simple"), 1)

    def test_enclosing_type(self):
        member = self.find("Simple", 0)
        self.assertEqual(csm.enclosing_type(SAMPLE, member.header_start), "Panel")

    def test_shape(self):
        self.assertEqual(csm.shape_of("get_Count", "Panel"), "getter")
        self.assertEqual(csm.shape_of("Panel", "Panel"), "ctor")
        self.assertEqual(csm.shape_of("Simple", "Panel"), "method")

    def test_param_count_handles_generics_and_nesting(self):
        self.assertEqual(csm.param_count("void F(Dictionary<int, List<string>> a)"), 1)
        self.assertEqual(csm.param_count("void F()"), 0)
        self.assertEqual(csm.param_count("void F(int a, (int, int) b)"), 2)


if __name__ == "__main__":
    unittest.main()
