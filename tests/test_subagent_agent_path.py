import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_agent_path import AgentPath  # noqa: E402


class AgentPathTest(unittest.TestCase):
    def test_parse_accepts_root_and_lower_snake_segments(self):
        root = AgentPath.parse("/root")
        child = AgentPath.parse("/root/researcher")
        grandchild = AgentPath.parse("/root/researcher/worker_1")

        self.assertEqual(str(root), "/root")
        self.assertEqual(str(child), "/root/researcher")
        self.assertEqual(str(grandchild), "/root/researcher/worker_1")
        self.assertTrue(root.is_root)
        self.assertFalse(child.is_root)

    def test_parse_rejects_non_canonical_paths(self):
        for bad_path in [
            "",
            "root",
            "/",
            "/foo",
            "/root/",
            "/root//worker",
            "/root/Researcher",
            "/root/a-b",
            "/root/has space",
            "/root/..",
            "/root/.hidden",
            "/root/worker.json",
        ]:
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(ValueError):
                    AgentPath.parse(bad_path)

    def test_name_and_parent(self):
        root = AgentPath.root()
        child = AgentPath.parse("/root/researcher")
        grandchild = AgentPath.parse("/root/researcher/worker_1")

        self.assertEqual(root.name, "root")
        self.assertIsNone(root.parent)
        self.assertEqual(child.name, "researcher")
        self.assertEqual(str(child.parent), "/root")
        self.assertEqual(grandchild.name, "worker_1")
        self.assertEqual(str(grandchild.parent), "/root/researcher")

    def test_join_appends_safe_child_segment(self):
        root = AgentPath.root()
        child = root.join("researcher")
        grandchild = child.join("worker_1")

        self.assertEqual(str(child), "/root/researcher")
        self.assertEqual(str(grandchild), "/root/researcher/worker_1")
        for bad_name in ["", "Upper", "a-b", "../escape", "/absolute", "has space"]:
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(ValueError):
                    child.join(bad_name)

    def test_resolve_accepts_absolute_or_direct_child_reference(self):
        current = AgentPath.parse("/root/researcher")

        self.assertEqual(str(current.resolve("/root/researcher/worker_1")), "/root/researcher/worker_1")
        self.assertEqual(str(current.resolve("worker_1")), "/root/researcher/worker_1")
        self.assertEqual(str(current.resolve("/root")), "/root")
        for bad_reference in ["../sibling", "worker/leaf", "Upper", "/root/bad-name"]:
            with self.subTest(bad_reference=bad_reference):
                with self.assertRaises(ValueError):
                    current.resolve(bad_reference)


if __name__ == "__main__":
    unittest.main()
