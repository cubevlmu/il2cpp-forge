import json
import struct
import tempfile
import unittest
from pathlib import Path

from forge.config import Config
from forge.unity import metadata as meta


def write_metadata(path: Path, version: int = 31, literals=("Hello", "World")) -> Path:
    literal_table_offset = 64
    literal_data_offset = literal_table_offset + 8 * len(literals)
    data = b""
    records = b""
    for text in literals:
        raw = text.encode("utf-8") + b"\x00"
        records += struct.pack("<II", len(raw), len(data))
        data += raw
    header = struct.pack("<Ii", meta.SANITY, version)
    header += struct.pack(
        "<6I",
        literal_table_offset,
        len(records),
        literal_data_offset,
        len(data),
        0,
        0,
    )
    blob = header + b"\x00" * (literal_table_offset - len(header))
    blob += records + data
    path.write_bytes(blob)
    return path


class MetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_header(self):
        path = write_metadata(self.root / "global-metadata.dat")
        header = meta.read_header(path)
        self.assertEqual(header.version, 31)
        self.assertEqual(header.literal_records, 2)
        self.assertIn("2022", header.unity_hint)

    def test_literals(self):
        path = write_metadata(self.root / "global-metadata.dat")
        self.assertEqual(meta.string_literals(path), ["Hello", "World"])

    def test_bad_magic(self):
        path = self.root / "bad.dat"
        path.write_bytes(b"\x00" * 64)
        with self.assertRaises(ValueError):
            meta.read_header(path)

    def test_detect_unity_version(self):
        blob = self.root / "globalgamemanagers"
        blob.write_bytes(b"\x00" * 100 + b"2022.3.14f1" + b"\x00" * 10)
        self.assertEqual(meta.detect_unity_version(blob), "2022.3.14f1")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_when_missing(self):
        config = Config.discover(self.root)
        self.assertIsNone(config.path)
        self.assertEqual(config.evidence_banner, "UNRESTORED EVIDENCE")

    def test_relative_paths_resolve_against_config(self):
        (self.root / "work").mkdir()
        path = self.root / "forge.json"
        path.write_text(
            json.dumps({"source": {"src_dir": "work/src"}, "gates": {"max_residue": 5}}),
            encoding="utf-8",
        )
        config = Config.load(path)
        self.assertEqual(config.path_of("source.src_dir"), (self.root / "work" / "src").resolve())
        self.assertEqual(config.get("gates.max_residue"), 5)
        self.assertEqual(config.get("gates.min_coverage"), 0.6)  # default survives merge

    def test_discover_walks_upwards(self):
        (self.root / "nested" / "deeper").mkdir(parents=True)
        (self.root / "forge.json").write_text("{}", encoding="utf-8")
        config = Config.discover(self.root / "nested" / "deeper")
        self.assertIsNotNone(config.path)
        self.assertEqual(config.path.parent, self.root.resolve())

    def test_save_roundtrip(self):
        config = Config()
        config.set("project.name", "Demo")
        target = config.save(self.root / "forge.json")
        reloaded = Config.load(target)
        self.assertEqual(reloaded.get("project.name"), "Demo")


if __name__ == "__main__":
    unittest.main()
