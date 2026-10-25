from pathlib import Path
import tempfile
import unittest

from refiner.standards import discover_project_standards


class ProjectStandardDiscoveryTests(unittest.TestCase):
    def test_discovers_repository_and_nested_project_standards_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            (root / ".git").mkdir(parents=True)
            (root / "PROJECT-STANDARDS.md").write_text("repository rules", encoding="utf-8")
            shared = root / ".skill-refiner" / "standards" / "quality.md"
            shared.parent.mkdir(parents=True)
            shared.write_text("shared rules", encoding="utf-8")
            project = root / "apps" / "media"
            (project / "PROJECT-STANDARDS.md").parent.mkdir(parents=True)
            (project / "PROJECT-STANDARDS.md").write_text("media rules", encoding="utf-8")
            nested = project / ".skill-refiner" / "standards" / "security.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("security rules", encoding="utf-8")
            target = project / "dist" / "csp-media"
            target.mkdir(parents=True)
            explicit = Path(temporary) / "extra.md"
            explicit.write_text("additional rules", encoding="utf-8")

            found = discover_project_standards(target, [str(explicit), str(shared)], root)

            self.assertEqual(found, [
                str((root / "PROJECT-STANDARDS.md").resolve()),
                str(shared.resolve()),
                str((project / "PROJECT-STANDARDS.md").resolve()),
                str(nested.resolve()),
                str(explicit.resolve()),
            ])

    def test_does_not_walk_unbounded_parent_directories_without_project_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            outside = parent / "PROJECT-STANDARDS.md"
            outside.write_text("parent rules", encoding="utf-8")
            standalone = parent / "isolated" / "skill"
            standalone.mkdir(parents=True)
            (parent / "isolated" / "pyproject.toml").write_text("[project]\nname='isolated'\n", encoding="utf-8")
            found = discover_project_standards(standalone, working_dir=standalone)
            self.assertEqual(found, [])

    def test_discovers_csp_media_project_standard_for_built_skill(self):
        project = Path(__file__).resolve().parents[1] / "csp-media"
        target = project / "dist" / "csp-media"
        found = discover_project_standards(target, working_dir=project)
        self.assertIn(str((project / "PROJECT-STANDARDS.md").resolve()), found)


if __name__ == "__main__":
    unittest.main()
