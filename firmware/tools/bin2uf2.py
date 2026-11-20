#!/usr/bin/env python3
"""Wrap a raw RP2040 flash image (objcopy -O binary of the firmware ELF) into a UF2 file.

    python tools/bin2uf2.py p65.bin p65.uf2

The image is placed at the start of the flash (0x10000000), in 256-byte blocks, with the
RP2040 family id, so the boot ROM's USB drive (BOOTSEL) accepts it.
"""
import struct
import sys

FLASH_BASE = 0x10000000
RP2040_FAMILY = 0xE48BFF56
BLOCK = 256


def bin2uf2(data, base=FLASH_BASE):
    data = data + b'\x00' * (-len(data) % BLOCK)
    blocks = len(data) // BLOCK
    out = bytearray()
    for n in range(blocks):
        payload = data[n * BLOCK:(n + 1) * BLOCK]
        header = struct.pack('<8I', 0x0A324655, 0x9E5D5157, 0x00002000, base + n * BLOCK, BLOCK, n, blocks, RP2040_FAMILY)
        out += header + payload + b'\x00' * (476 - BLOCK) + struct.pack('<I', 0x0AB16F30)
    return bytes(out)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with open(sys.argv[1], 'rb') as f:
        image = f.read()
    with open(sys.argv[2], 'wb') as f:
        f.write(bin2uf2(image))
