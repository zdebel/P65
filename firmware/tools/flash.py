#!/usr/bin/env python3
"""Copy a UF2 file to a Raspberry Pi Pico that is in BOOTSEL mode (the USB drive "RPI-RP2").

    python tools/flash.py out/p65.uf2

Windows only for now (finds the drive by its volume label); Linux / macOS are not done yet.
"""
import ctypes
import os
import shutil
import sys

LABEL = 'RPI-RP2'


def find_drive_windows():
    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = chr(ord('A') + i) + ':' + os.sep
        name = ctypes.create_unicode_buffer(261)
        if kernel32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0) and name.value == LABEL:
            return root
    return None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    uf2 = sys.argv[1]
    if not os.path.isfile(uf2):
        sys.exit('%s not found - build first: make' % uf2)
    if os.name != 'nt':
        sys.exit('make flash is only implemented for Windows so far. Copy %s to the %s drive by hand.' % (uf2, LABEL))
    drive = find_drive_windows()
    if not drive:
        sys.exit('No %s drive found. Put the Pico into BOOTSEL mode:\n'
                 '  1. unplug the USB cable\n'
                 '  2. hold the BOOTSEL button, plug the cable in, release the button\n'
                 '  3. run make flash again' % LABEL)
    shutil.copy(uf2, drive)
    print('Copied %s to %s - the Pico restarts by itself.' % (os.path.basename(uf2), drive))


if __name__ == '__main__':
    main()
