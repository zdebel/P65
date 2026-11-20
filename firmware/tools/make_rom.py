#!/usr/bin/env python3
"""Assemble the P65 ROM (rom/p65_basic.a65 + fpp65.a65 + storage.a65) with ca65/ld65
and write include/rom.h, the 32512-byte image the firmware copies to $8100.

    python tools/make_rom.py [--no-storage] [--bin out.bin] [--check]

--bin    also keep the raw image (the emulator loads it with --rom)
--check  do not write anything, only report whether include/rom.h is up to date
Needs ca65 and ld65 (cc65 package) on PATH.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asm import assemble        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROM_SIZE = 32768 - 256


def rom_header(data):
    data = data[:ROM_SIZE].ljust(ROM_SIZE, b'\xff')
    lines = ['#include <stdint.h>', '', 'const uint8_t ROM_CONTENT[%d] = {' % ROM_SIZE]
    for i in range(0, ROM_SIZE, 16):
        chunk = ', '.join('0x%02X' % b for b in data[i:i + 16])
        last = i + 16 >= ROM_SIZE
        lines.append(' %s%s /* 0x%04X */' % (chunk, '' if last else ',', i))
    lines.append('};')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--no-storage', action='store_true', help='build without the storage commands (STORAGE=0)')
    ap.add_argument('--bin', help='also write the raw ROM image to this file')
    ap.add_argument('--check', action='store_true', help='only check that include/rom.h matches the sources')
    args = ap.parse_args()

    data = assemble(os.path.join(ROOT, 'rom', 'p65_basic.a65'), ['STORAGE=0'] if args.no_storage else [])
    if len(data) > ROM_SIZE:
        sys.exit('ROM image is %d bytes, the limit is %d' % (len(data), ROM_SIZE))
    header = rom_header(data)
    path = os.path.join(ROOT, 'include', 'rom.h')
    if args.check:
        with open(path, 'r') as f:
            same = f.read() == header
        print('include/rom.h is %s' % ('up to date' if same else 'OUT OF DATE'))
        sys.exit(0 if same else 1)
    with open(path, 'w', newline='\n') as f:
        f.write(header)
    if args.bin:
        with open(args.bin, 'wb') as f:
            f.write(data.ljust(ROM_SIZE, b'\xff'))
    print('wrote include/rom.h (%d bytes of code, %d free)' % (len(data), ROM_SIZE - len(data)))


if __name__ == '__main__':
    main()
