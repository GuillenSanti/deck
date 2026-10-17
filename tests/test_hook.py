import os
import subprocess
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HookTests(unittest.TestCase):
    def test_pre_commit_refuses_when_gitleaks_is_missing(self):
        env = {"PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            ["/bin/sh", os.path.join(ROOT, ".githooks", "pre-commit")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to commit unchecked files", result.stderr)


if __name__ == "__main__":
    unittest.main()
