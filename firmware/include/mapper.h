#ifndef _MAPPER_H
#define _MAPPER_H

#include <stdint.h>
#include "pico/platform.h"

/* ---------------------------------------------------------------------------
   THE memory map (documented in docs/MEMORY_MAP.md). Everything in the firmware
   (and the storage code) is written in terms of these names, so moving things
   (more RAM, I/O somewhere else) means changing this block - plus the matching
   equates at the top of rom/p65_basic.a65 and emulator/memmap.py.
   The I/O area must start on a 256-byte page boundary.
   ------------------------------------------------------------------------- */
#define RAM_START   0x0000
#define RAM_END     0x7FFF      /* last RAM address the CPU may write           */
#define IO_START    0x8000      /* I/O page: registers below are offsets here    */
#define IO_END      0x80FF
#define ROM_START   0x8100      /* ROM_CONTENT is copied here                    */
#define ROM_END     0xFFFF

/* I/O registers (offsets from IO_START) */
#define IO_REG_LEDS     0x00    /* W: bits 0-2 = LED0..2            R: 0xF8 | leds */
#define IO_REG_KEY      0x01    /* bit 7 = key waiting, write acknowledges        */
#define IO_REG_VDATA    0x02    /* VDP data                                       */
#define IO_REG_VCTRL    0x03    /* VDP: write = address / register, read = status */
#define IO_REG_ROMCTL   0x04    /* W bit0: 1 = ROM off ($8100-$FFFF becomes RAM), 0 = ROM on (image restored)
                                   R bit0 = ROM off, bit7 = busy (restoring the image) */
#define IO_REG_STORAGE  0x10    /* storage mailbox, see storage.h (STO_REG_COUNT bytes) */

#define IO_LEDS     (IO_START + IO_REG_LEDS)
#define IO_KEY      (IO_START + IO_REG_KEY)
#define IO_VDATA    (IO_START + IO_REG_VDATA)
#define IO_VCTRL    (IO_START + IO_REG_VCTRL)
#define IO_ROMCTL   (IO_START + IO_REG_ROMCTL)
#define IO_STORAGE  (IO_START + IO_REG_STORAGE)

#define ROMCTL_OFF   0x01       /* bit 0: ROM disabled, the area above the I/O page is RAM */
#define ROMCTL_BUSY  0x80       /* bit 7 (read): the ROM image is being restored; wait */

/* The whole 64 KB address space of the 65C02. Reads are served straight from
   here (also the I/O registers: their current value is kept in the array). */
extern uint8_t cpu_memory[65536];
extern volatile uint8_t rom_ram;    /* 1 = ROM disabled: writes above IO_END go to cpu_memory */

void mapper_init(void);             /* clear RAM, copy the ROM image */
void mapper_init_basic(void);       /* put the demo BASIC program into RAM (Ctrl+=) */
void mapper_rom_poll(void);         /* core 0: finish a pending ROM restore */

/* core 1: the CPU wrote I/O register `addr` (offset from IO_START). The VDP data
   and keyboard registers are handled inline in cpu_core1_loop(). */
void mapper_io_write(uint8_t addr, uint8_t val);

#endif
