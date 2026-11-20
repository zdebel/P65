"""Compiles and runs tests/fs_test.c (the flash file system with power-failure injection)
with the host C compiler. Skipped when there is none."""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')   # 'cc' first: MSYS2's gcc handles longjmp, older MinGW builds crash


@unittest.skipUnless(CC, 'no C compiler')
class TestFsC(unittest.TestCase):
    def test_fs_with_power_failures(self):
        exe = os.path.join(tempfile.mkdtemp(), 'fs_test.exe')
        subprocess.check_call([CC, '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-I', os.path.join(ROOT, 'include'),
                               os.path.join(ROOT, 'tests', 'fs_test.c'), os.path.join(ROOT, 'src', 'fs.c'), '-static', '-o', exe])     # static: no runtime DLLs needed
        r = subprocess.run([exe], capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('0 failures', r.stdout)


if __name__ == '__main__':
    unittest.main()
