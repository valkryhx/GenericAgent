from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingManifestTest(unittest.TestCase):
    def test_workflow_authoring_is_package_data_not_a_python_package(self):
        pyproject = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        setuptools = pyproject['tool']['setuptools']
        packages = set(setuptools['packages'])
        resources = setuptools['package-data']['genericagent.resources']

        self.assertNotIn('genericagent.resources.skills.workflow-authoring', packages)
        self.assertNotIn('genericagent.resources.skills.workflow-authoring.references', packages)
        self.assertIn('skills/workflow-authoring/SKILL.md', resources)
        self.assertIn('skills/workflow-authoring/references/api.md', resources)
        self.assertIn('skills/workflow-authoring/references/artifacts.md', resources)
        self.assertIn('skills/workflow-authoring/references/examples.md', resources)
        self.assertIn('skills/workflow-authoring/references/resume.md', resources)
        self.assertIn('skills/workflow-authoring/references/testing.md', resources)


if __name__ == '__main__':
    unittest.main()
