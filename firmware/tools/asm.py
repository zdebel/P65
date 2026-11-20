#!/usr/bin/env python3
"""Assemble a 65C02 source with ca65/ld65 (cc65 package, 2.19 or newer on PATH).

    python tools/asm.py examples/vsync.a65 [-o vsync.bin] [-D NAME=VALUE ...]

Writes a flat binary ('-t none'), the same way the ROM is built (make_rom.py).
Use .setcpu "W65C02" in the source for WAI / STP and the other 65C02 opcodes.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def assemble(source, defines=()):
    """Return the assembled binary of `source` (bytes). Includes are searched next to the source."""
    ca65, ld65 = shutil.which('ca65'), shutil.which('ld65')
    if not (ca65 and ld65):
        sys.exit('ca65 / ld65 not found (install the cc65 package)')
    source = os.path.abspath(source)
    with tempfile.TemporaryDirectory() as d:
        obj, out = os.path.join(d, 'a.obj'), os.path.join(d, 'a.bin')
        cmd = [ca65, '-I', os.path.dirname(source), '-o', obj] + [a for x in defines for a in ('-D', x)] + [source]
        subprocess.check_call(cmd)
        subprocess.check_call([ld65, '-D__STACKSTART__=65535', '-D__STACKSIZE__=0', '-t', 'none', '-o', out, obj],
                              stderr=subprocess.DEVNULL)
        with open(out, 'rb') as f:
            return f.read()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('source')
    ap.add_argument('-o', '--output', help='output file (default: the source name with .bin)')
    ap.add_argument('-D', dest='defines', action='append', default=[], help='NAME=VALUE passed to ca65')
    args = ap.parse_args()
    data = assemble(args.source, args.defines)
    out = args.output or os.path.splitext(os.path.basename(args.source))[0] + '.bin'
    with open(out, 'wb') as f:
        f.write(data)
    print('%s: %d bytes' % (out, len(data)))


if __name__ == '__main__':
    main()
