import json
import pathlib
import sys
import tempfile
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.memory_import import import_summary, read_legacy_entries


class MemoryImportTests(unittest.TestCase):
    def test_profile_atomic_ids_and_records_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "user_profile.json").write_text(json.dumps({
                "core_traits": ["A", "B"], "communication_style": "C",
            }), encoding="utf-8")
            (root / "memory_records.json").write_text(json.dumps([
                {"id": "legacy-1", "text": "Record", "type": "special", "importance": 0.9, "status": "active"},
            ]), encoding="utf-8")
            (root / "memory.md").write_text("- This note is superseded by records\n", encoding="utf-8")
            first = read_legacy_entries(root)
            second = read_legacy_entries(root)
            self.assertEqual([item.id for item in first], [item.id for item in second])
            self.assertEqual(import_summary(first)["total"], 4)
            self.assertEqual({item.canonical_text for item in first}, {"A", "B", "C", "Record"})
            self.assertEqual(next(item for item in first if item.canonical_text == "Record").memory_type, "special")


if __name__ == "__main__":
    unittest.main()
