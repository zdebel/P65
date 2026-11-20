#include "mapper.h"
#include "basic_demo.h"
#include "rom.h"
#include "video.h"
#include <string.h>
#include "keyboard.h"
#include "storage.h"
#include "storage_fw.h"
#include "pico/platform.h"
#include "hardware/sync.h"

/* Memory map of the P65: see mapper.h and docs/MEMORY_MAP.md. */

/* the whole 64 KB address space of the 65C02 */
uint8_t cpu_memory[65536] __attribute__((aligned(4)));

volatile uint8_t rom_ram = 0;
static volatile uint8_t rom_restore_pending = 0;

/* core 1 (bus loop): the CPU wrote ROMCTL. Only flags are changed here; the
   32 KB copy is far too long for a bus cycle and is done by core 0. */
static void __not_in_flash_func(romctl_write)(uint8_t val)
{
    if (val & ROMCTL_OFF) {
        if (rom_restore_pending) return;        /* still restoring: ignore */
        rom_ram = 1;
        cpu_memory[IO_ROMCTL] = ROMCTL_OFF;
    } else if (rom_ram) {
        rom_ram = 0;
        cpu_memory[IO_ROMCTL] = ROMCTL_BUSY;    /* visible to the CPU before its next read */
        __dmb();
        rom_restore_pending = 1;
    }
}

void mapper_rom_poll(void)
{
    if (rom_restore_pending) {
        memcpy(&cpu_memory[ROM_START], ROM_CONTENT, sizeof(ROM_CONTENT));
        __dmb();
        cpu_memory[IO_ROMCTL] = 0;
        rom_restore_pending = 0;
    }
}

void mapper_init(void)
{
    memset(cpu_memory, 0, ROM_START);       /* RAM and the I/O page */
    memcpy(&cpu_memory[ROM_START], ROM_CONTENT, sizeof(ROM_CONTENT));
    rom_ram = 0;
    rom_restore_pending = 0;
    cpu_memory[IO_LEDS] = keyboard_get_leds();
    cpu_memory[IO_KEY] = 0;
}

void mapper_init_basic(void)
{
    const uint16_t start = BASIC_DEMO_START;
    const uint16_t end = start + sizeof basic_demo;     /* first free byte after the program */

    /* zero-page pointers of the BASIC ROM: start and end of the program (and the
       copy of the end at $AF) */
    cpu_memory[0x0067] = start & 0xff;
    cpu_memory[0x0068] = start >> 8;
    cpu_memory[0x0069] = end & 0xff;
    cpu_memory[0x006A] = end >> 8;
    cpu_memory[0x00AF] = end & 0xff;
    cpu_memory[0x00B0] = end >> 8;
    memcpy(&cpu_memory[start], basic_demo, sizeof basic_demo);
}

void __not_in_flash_func(mapper_io_write)(uint8_t addr, uint8_t val)
{
    switch (addr) {
        case IO_REG_LEDS:   keyboard_set_leds(val); break;
        case IO_REG_VCTRL:  video_write_addr(val); break;
        case IO_REG_ROMCTL: romctl_write(val); break;
        default:
            if (addr >= IO_REG_STORAGE && addr < IO_REG_STORAGE + STO_REG_COUNT)
                storage_reg_write((uint8_t)(addr - IO_REG_STORAGE), val);
            break;
    }
}
