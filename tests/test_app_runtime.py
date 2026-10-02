import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app_runtime

class PortableRuntimeTests(unittest.TestCase):
    def test_frozen_launch_uses_executable_folder_and_external_catalog_overrides(self):
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as folder:
                with patch.object(app_runtime.sys, 'frozen', True, create=True), patch.object(app_runtime.sys, 'executable', str(Path(folder)/'EntropiaTracker.exe')):
                    app_runtime.initialize_runtime()
                self.assertEqual(Path.cwd().resolve(), Path(folder).resolve())
                self.assertEqual(app_runtime.reference_file('skills.json'), Path(app_runtime.__file__).resolve().with_name('skills.json'))
                Path('skills.json').write_text('[]')
                self.assertEqual(app_runtime.reference_file('skills.json'), Path('skills.json'))
                os.chdir(previous)
        finally: os.chdir(previous)
