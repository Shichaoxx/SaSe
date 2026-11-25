"""Install and uninstall the extension via Pi's project package manager."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PiPackageTest(unittest.TestCase):
    def test_local_install_and_remove(self):
        pi = ROOT / "node_modules/.bin/pi"
        package = ROOT / "plugins/pi"
        self.assertTrue(pi.is_file(), "install the pinned runtime with npm ci")
        self.assertTrue((package / "package.json").is_file())
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            env = {
                "PATH": os.environ["PATH"],
                "HOME": directory,
                "PI_CODING_AGENT_DIR": str(Path(directory) / "agent"),
                "PI_OFFLINE": "1",
            }

            def run(command):
                subprocess.run([pi, command, str(package), "--local", "--approve"], check=True,
                               cwd=project, env=env, capture_output=True, text=True, timeout=20)

            settings = project / ".pi/settings.json"
            run("install")
            sources = json.loads(settings.read_text())["packages"]
            self.assertEqual(1, len(sources))
            self.assertEqual(package.resolve(), (settings.parent / sources[0]).resolve())
            run("remove")
            self.assertFalse(settings.exists() and json.loads(settings.read_text()).get("packages"))
            self.assertTrue((package / "index.ts").is_file())


if __name__ == "__main__":
    unittest.main()
