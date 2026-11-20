#!/usr/bin/env python3
"""Build tests/ref/p65store.dll (or .so / .dylib): the firmware's C storage code as a shared
library. It is the REFERENCE the Python port in emulator/p65fs.py is compared against
(tests/test_py_vs_c.py). Not needed to run the emulator. Original text:

the firmware's storage code
(fs.c, storage_core.c, usb_proto.c + p65store.c) for the
emulator. Needs a C compiler (gcc / clang) on the PATH, e.g. from MSYS2.

    python build_storelib.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = '.dll' if sys.platform == 'win32' else ('.dylib' if sys.platform == 'darwin' else '.so')
OUT = os.path.join(ROOT, 'tests', 'ref', 'p65store' + EXT)
SRC_DIR, INC_DIR = os.path.join(ROOT, 'src'), os.path.join(ROOT, 'include')
SOURCES = [os.path.join(ROOT, 'tools', 'p65store.c'), os.path.join(ROOT, 'src', 'fs.c'),
           os.path.join(ROOT, 'src', 'storage_core.c'), os.path.join(ROOT, 'src', 'usb_proto.c')]


def main():
    cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
    if not cc:
        sys.exit('no C compiler found (install MSYS2 gcc or any gcc/clang)')
    cmd = [cc, '-O2', '-Wall', '-Wextra', '-shared', '-I', INC_DIR, '-I', SRC_DIR, '-o', OUT] + SOURCES
    if sys.platform != 'win32':
        cmd.insert(2, '-fPIC')
    else:
        cmd += ['-static-libgcc']
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print(' '.join(cmd))
    subprocess.check_call(cmd)
    print('built', OUT)


if __name__ == '__main__':
    main()
