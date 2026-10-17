import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
INSTALL = ROOT / "install.sh"
SCRIPTS = ("dash.py", "agents-history.py", "pr_review.py", "branch_diff.py")


class InstallTests(unittest.TestCase):
    def run_install(self, home):
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        return subprocess.run(["/bin/sh", str(INSTALL)], env=env, capture_output=True, text=True)

    def test_install_refuses_to_replace_a_file_without_partial_links(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            tmux = home / ".tmux"
            tmux.mkdir()
            existing = tmux / SCRIPTS[0]
            existing.write_text("personal command\n")

            result = self.run_install(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(existing.read_text(), "personal command\n")
            self.assertFalse((tmux / SCRIPTS[1]).exists())

    def test_install_refuses_to_replace_a_link_to_another_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            tmux = home / ".tmux"
            tmux.mkdir()
            other = home / "other-dash.py"
            other.write_text("other checkout\n")
            existing = tmux / SCRIPTS[0]
            existing.symlink_to(other)

            result = self.run_install(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(existing.resolve(), other.resolve())
            self.assertFalse((tmux / SCRIPTS[1]).exists())

    def test_install_creates_links_when_destinations_are_free(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            result = self.run_install(home)

            self.assertEqual(result.returncode, 0, result.stderr)
            for script in SCRIPTS:
                self.assertEqual((home / ".tmux" / script).resolve(), ROOT / script)


if __name__ == "__main__":
    unittest.main()
