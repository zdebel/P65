"""ROMCTL ($8004): switching the ROM off makes $8100-$FFFF RAM, switching it on
restores the ROM image (after a short busy period). Hand-assembled program, no
ca65 needed.
Run:  python -m unittest discover -s tests -v"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, '..', 'emulator'))

from machine import P65, CPU_CLOCK      # noqa: E402
from memmap import IO_ROMCTL, ROM_START, ROM_SIZE, IO_END   # noqa: E402

PROG = bytes([
    0x78,                           # sei
    0xA9, 0x77, 0x8D, 0x00, 0x81,   # lda #$77 : sta $8100      (ROM on: ignored)
    0xAD, 0x00, 0x81, 0x8D, 0x05, 0x06,   # lda $8100 : sta $0605
    0xA9, 0x01, 0x8D, 0x04, 0x80,   # lda #1 : sta $8004         ROM off
    0xAD, 0x04, 0x80, 0x8D, 0x06, 0x06,   # lda $8004 : sta $0606  (status while off)
    0xA9, 0x5A,                     # lda #$5A
    0x8D, 0x00, 0x81,               # sta $8100
    0x8D, 0xFF, 0xFF,               # sta $FFFF
    0x8D, 0x00, 0x90,               # sta $9000
    0xEE, 0x00, 0x90,               # inc $9000   (RMW above the I/O page)
    0xAD, 0x00, 0x81, 0x8D, 0x00, 0x06,   # lda $8100 : sta $0600
    0xAD, 0xFF, 0xFF, 0x8D, 0x01, 0x06,   # lda $FFFF : sta $0601
    0xAD, 0x00, 0x90, 0x8D, 0x07, 0x06,   # lda $9000 : sta $0607
    0x9C, 0x04, 0x80,               # stz $8004                  ROM on
    0xAD, 0x04, 0x80, 0x30, 0xFB,   # wait: lda $8004 : bmi wait
    0xAD, 0x00, 0x81, 0x8D, 0x02, 0x06,   # lda $8100 : sta $0602
    0xAD, 0x00, 0x90, 0x8D, 0x03, 0x06,   # lda $9000 : sta $0603
    0xAD, 0x04, 0x80, 0x8D, 0x04, 0x06,   # lda $8004 : sta $0604
    0xDB,                           # stp
])


class TestRomCtl(unittest.TestCase):
    def setUp(self):
        self.rom = bytes((i * 7 + 3) & 0xFF for i in range(ROM_SIZE))
        self.p = P65()
        self.p.load_rom(self.rom)
        self.p.power_on()

    def run_prog(self):
        m = self.p.mem
        m[0x0800:0x0800 + len(PROG)] = PROG
        self.p.cpu.pc = 0x0800
        self.p.run(CPU_CLOCK // 20)
        self.assertTrue(self.p.cpu.halted, 'program did not reach STP')

    def test_off_write_on_restore(self):
        self.run_prog()
        m = self.p.mem
        self.assertEqual(m[0x0605], self.rom[0], 'write to ROM while it is on must be ignored')
        self.assertEqual(m[0x0606], 0x01)
        self.assertEqual(m[0x0600], 0x5A)       # RAM at $8100 while off
        self.assertEqual(m[0x0601], 0x5A)       # and at $FFFF
        self.assertEqual(m[0x0607], 0x5B)       # RMW at $9000
        self.assertEqual(m[0x0602], self.rom[0])                      # ROM image is back
        self.assertEqual(m[0x0603], self.rom[0x9000 - ROM_START])
        self.assertEqual(m[0x0604], 0x00)       # not busy, ROM on
        self.assertEqual(bytes(m[ROM_START:0x10000]), self.rom)
        self.assertFalse(self.p.rom_ram)

    def test_io_page_stays_io_when_rom_off(self):
        self.p._wr_io(IO_ROMCTL, 1)
        self.assertTrue(self.p.rom_ram)
        self.p._wr_io(IO_END + 1, 0x11)
        self.assertEqual(self.p.mem[IO_END + 1], 0x11)
        before = self.p.mem[0x8005]
        self.p._wr_io(0x8005, 0x99)             # unused I/O offset: still ignored
        self.assertEqual(self.p.mem[0x8005], before)

    def test_busy_until_restored(self):
        self.p._wr_io(IO_ROMCTL, 1)
        self.p.mem[0x8100] = 0xEE
        self.p._wr_io(IO_ROMCTL, 0)
        self.assertEqual(self.p.mem[IO_ROMCTL], 0x80)
        self.p.cpu.halted = 2                   # STP: only time passes
        self.p.run(CPU_CLOCK // 100)
        self.assertEqual(self.p.mem[IO_ROMCTL], 0x00)
        self.assertEqual(self.p.mem[0x8100], self.rom[0])


if __name__ == '__main__':
    unittest.main()
