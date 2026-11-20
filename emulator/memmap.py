"""The memory map of the P65 in one place.

Keep in sync with include/mapper.h (firmware) and the equates at the top of
asm_examples/testrom1.a65 (ROM): all three use the same names. Everything else
in the emulator is written in terms of these constants, so moving the I/O page
or changing the amount of RAM means editing this file (and those two).
"""

RAM_START = 0x0000
RAM_END = 0x7FFF            # last address the CPU may write
IO_START = 0x8000           # I/O page, 256 bytes, page aligned
IO_END = 0x80FF
ROM_START = 0x8100          # the ROM image is copied here
ROM_END = 0xFFFF
ROM_SIZE = ROM_END - ROM_START + 1

# I/O registers: offsets from IO_START
IO_REG_LEDS = 0x00          # W: bits 0-2 = LED0..2           R: 0xF8 | leds
IO_REG_KEY = 0x01           # bit 7 = key waiting, write acknowledges
IO_REG_VDATA = 0x02         # VDP data
IO_REG_VCTRL = 0x03         # VDP: write = address / register, read = status
IO_REG_STORAGE = 0x10       # storage mailbox (see storage.py)

IO_LEDS = IO_START + IO_REG_LEDS
IO_KEY = IO_START + IO_REG_KEY
IO_VDATA = IO_START + IO_REG_VDATA
IO_VCTRL = IO_START + IO_REG_VCTRL
IO_STORAGE = IO_START + IO_REG_STORAGE

assert IO_START % 0x100 == 0 and IO_END == IO_START + 0xFF
assert IO_VDATA % 2 == 0 and IO_VCTRL == IO_VDATA + 1       # the CPU core reads both through one test
assert RAM_END < IO_START < ROM_START
